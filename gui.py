#!/usr/bin/env python3
"""Minimal GTK4/libadwaita UI: type a URL, pick the right SteamGridDB
match, create the Steam shortcut. Styled like
github.com/unrud/video-downloader -- single window, no bells and
whistles. First run shows a 3-step onboarding check (Steam, Edge,
SGDB key) before the main window.
"""
import concurrent.futures
import re
import subprocess
import threading
import urllib.request
from datetime import date
from urllib.parse import urlparse

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

import create_webapp as cw  # noqa: E402
import config  # noqa: E402
import edge_launcher  # noqa: E402
import host_exec  # noqa: E402
import sgdb_client as sgdb  # noqa: E402
import shortcuts_export  # noqa: E402
import steam_paths  # noqa: E402
import steam_restart  # noqa: E402
from streaming_services import STREAMING_SERVICES  # noqa: E402

APP_NAME = "Gridge"
GRIDGE_VERSION = "1.3.4"
GITHUB_URL = "https://github.com/ScarletPachydermDev/Gridge"
ISSUES_URL = "https://github.com/ScarletPachydermDev/Gridge/issues"
SGDB_KEY_URL = "https://steamgriddb.com/profile/preferences/api"
DONATE_URL = "https://github.com/ScarletPachydermDev/gridge#donate"
EDGE_REASON_TEXT = (
    "Edge is the only Chromium browser on Linux licensed for Dolby "
    "Digital Plus/Atmos audio -- other browsers can't play it."
)

# Only a close button -- no minimize/maximize -- on every window in the app.
NO_MINMAX_DECORATION_LAYOUT = ":close"

# Explicit color instead of relying on the theme's "success" semantic class --
# that class doesn't render as green consistently across every desktop
# environment/theme (confirmed: no visible color on one test machine).
# .zebra-odd/.zebra-even stripe the results list (real matches and the
# empty placeholder rows alike) so the reserved space doesn't look like
# a dead gray box before a search happens. Selector matches the
# boxed-list theme CSS's own specificity (list row.foo) rather than a
# bare class, since a bare class alone wasn't reliably overriding it.
_STATUS_CSS = b"""
.status-ok { color: #26a269; }
list row.zebra-odd:not(:selected) { background-color: alpha(currentColor, 0.03); }
list row.zebra-even:not(:selected) { background-color: alpha(currentColor, 0.07); }
.artwork-skeleton { background-color: alpha(currentColor, 0.12); border-radius: 6px; }
.artwork-cell { border-radius: 6px; border: 3px solid transparent; }
.artwork-cell.selected { border-color: #3584e4; }
/* Real cells are wrapped in a Gtk.Button for click handling -- the
   "flat" class only strips its background/border, not its internal
   padding, which was inflating the gap between real thumbnails well
   past the row's own spacing (confirmed: skeleton cells, plain boxes
   with no button, had no such gap). Zero it out entirely. */
.artwork-button { padding: 0; margin: 0; min-width: 0; min-height: 0; }
.artwork-check {
  background-color: #3584e4;
  color: white;
  border-radius: 999px;
  min-width: 18px;
  min-height: 18px;
  font-size: 11px;
}
/* Strips the ScrolledWindow's own default border/shadow/background so
   a real cell (Overlay > this > Picture) matches a skeleton cell (a
   plain Box) exactly instead of rendering a hair different. */
.artwork-clip, .artwork-clip > viewport { border: none; background: none; box-shadow: none; padding: 0; }
/* Reserves the per-row horizontal scrollbar's height unconditionally
   (see the scroller's hscrollbar_policy=ALWAYS) without permanently
   showing a visible bar for rows that don't actually need to scroll --
   toggled by _update_row_scrollbar_visibility whenever a row's content
   changes. */
.artwork-row-scrollbar-hidden { opacity: 0; }
"""

# The 5 SGDB artwork categories the picker shows, each as its own
# horizontally-scrolling row: (internal basename matching
# create_webapp.GRID_FILENAMES, display title, cell width, cell height).
# Cell sizes roughly follow each category's real aspect ratio (grids
# 600x900/920x430, heroes ~1920x620, logos/icons squarer) scaled down to
# a picker-thumbnail size -- not pixel-exact, just visually distinct
# enough to tell the categories apart at a glance.
ARTWORK_CATEGORIES = [
    ("grid_vertical", "Vertical Grid", 170, 255),
    ("grid_horizontal", "Horizontal Grid", 260, 121),
    ("hero", "Hero", 320, 104),
    ("logo", "Logo", 160, 100),
    ("icon", "Icon", 100, 100),
]
_ARTWORK_ROW_SPACING = 4

# Overhead subtracted from the window's live, actual width/height
# (self.get_width()/get_height(), not a fixed guess) to estimate how
# much is left for the artwork panel's rows and the results list --
# left column's fixed width + separator, and each side's own panel
# margins/chrome (header bar, url entry, hint label, buttons/status/
# pending label below the results list).
_ARTWORK_PANEL_OVERHEAD = 460 + 40 + 16 + 24
_RESULTS_ROW_HEIGHT_ESTIMATE = 46
_RESULTS_LIST_CHROME_OVERHEAD = 260

# Thumbnail downloads no longer go through a per-image raw Thread --
# with the candidate-list cap removed, a popular title's dozens of
# results would otherwise fire off that many simultaneous connections
# at once. Shared across all categories/searches so total concurrency
# stays bounded regardless of how many rows are loading at the same time.
_THUMBNAIL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=6, thread_name_prefix="gridge-thumb")


def _install_status_css():
    provider = Gtk.CssProvider()
    provider.load_from_data(_STATUS_CSS)
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )


def guess_name_from_url(url):
    if "://" not in url:
        url = f"https://{url}"
    host = urlparse(url).netloc
    host = host.removeprefix("www.")
    # The registrable-domain label, not just the first one: open.spotify.com
    # and web.stremio.com should guess "Spotify"/"Stremio", not the
    # "open"/"web" subdomain in front of it.
    parts = host.split(".")
    base = parts[-2] if len(parts) >= 2 else parts[0]
    return base.replace("-", " ").title()


def _is_plain_youtube(resolved):
    """True only for the main youtube.com site -- not tv.youtube.com
    (the separate paid live-TV service) or a youtu.be video link, since
    the TV/leanback interface swap only makes sense for browsing the
    regular site."""
    if not resolved.url:
        return False
    return urlparse(resolved.url).netloc.removeprefix("www.") == "youtube.com"


def _looks_like_url(text):
    candidate = text if "://" in text else f"https://{text}"
    host = urlparse(candidate).netloc
    return " " not in text and "." in host


class ResolvedInput:
    """Result of interpreting the URL bar's free-text input. url/name are
    None when the text isn't a recognized service name and doesn't look
    like a URL either -- warning then explains why."""

    def __init__(self, url=None, name=None, sgdb_id=None, warning=None):
        self.url = url
        self.name = name
        self.sgdb_id = sgdb_id
        self.warning = warning


def _match_streaming_service(key):
    """Resolve a partial name (e.g. "Prime" for "Prime Video", "hbo" for
    "hbo max") without requiring the exact full alias -- but only once
    there's a single matching *entry*. Several aliases can point at the
    identical (domain, name) tuple (e.g. "prime video" and "amazon
    prime video" are the same service) -- that's not ambiguous, it's
    the same answer twice. Prefix matches are tried first and preferred
    over substring matches, so "hbo" resolves once it's an unambiguous
    start rather than waiting for a coincidental substring elsewhere."""
    starts = {STREAMING_SERVICES[k] for k in STREAMING_SERVICES if k.startswith(key)}
    if len(starts) == 1:
        return next(iter(starts))
    contains = {STREAMING_SERVICES[k] for k in STREAMING_SERVICES if key in k}
    if len(contains) == 1:
        return next(iter(contains))
    return None


def resolve_url_input(text):
    text = text.strip()
    if not text:
        return ResolvedInput()

    known = STREAMING_SERVICES.get(text.lower()) or _match_streaming_service(text.lower())
    if known:
        domain, name, sgdb_id = known
        return ResolvedInput(url=f"https://{domain}", name=name, sgdb_id=sgdb_id)

    if _looks_like_url(text):
        url = text if "://" in text else f"https://{text}"
        return ResolvedInput(url=url, name=guess_name_from_url(url))

    return ResolvedInput(warning=f'"{text}" isn\'t a recognized service name or a URL')


def _all_requirements_met():
    """Live-checked every startup, not cached -- the user may have
    uninstalled Steam/Edge or cleared the SGDB key since the app last
    ran, and onboarding should reappear if so rather than trusting a
    stale "already done" flag."""
    try:
        steam_paths.find_steam_root()
    except steam_paths.SteamNotFoundError:
        return False
    try:
        edge_launcher.find_edge()
    except edge_launcher.EdgeNotFoundError:
        return False
    return bool(config.get_sgdb_api_key())


_PROGRESS_RE = re.compile(r"(\d+)%")


def _flatpak_install_user(app_id, progress_callback=None):
    """Install a Flatpak app in --user scope, adding a user-level flathub
    remote first if one doesn't already exist. A system-wide install
    needs polkit authorization that regular user accounts often don't
    have (confirmed failing outright, no auth prompt, on both a Fedora
    desktop and a real Steam Deck -- SteamOS blocks system-scope changes
    while its read-only OS protection is enabled, which it is by
    default); --user sidesteps all of this, confirmed working on a real
    Deck. Returns (ok, output_tail).

    If given, progress_callback(fraction) is called synchronously from
    this (caller's) thread as flatpak's own percentage progress updates
    are parsed from its output -- callers running this in a background
    thread need to marshal it back to the main thread themselves (e.g.
    via GLib.idle_add) before touching any widget."""
    subprocess.run(
        host_exec.wrap(
            ["flatpak", "remote-add", "--user", "--if-not-exists", "flathub", "https://flathub.org/repo/flathub.flatpakrepo"]
        ),
        capture_output=True,
    )
    process = subprocess.Popen(
        host_exec.wrap(["flatpak", "install", "--user", "-y", "flathub", app_id]),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    # flatpak's CLI progress bar uses \r to overwrite the same line in a
    # terminal, but falls back to \n-separated updates when stdout isn't a
    # TTY (as it is here, piped) -- treat either as a line terminator so
    # this works regardless of which one shows up.
    tail_lines = []
    buf = ""
    while True:
        chunk = process.stdout.read(1)
        if chunk == "":
            break
        if chunk in ("\r", "\n"):
            if buf.strip():
                tail_lines.append(buf)
                match = _PROGRESS_RE.search(buf)
                if match and progress_callback:
                    progress_callback(int(match.group(1)) / 100.0)
            buf = ""
        else:
            buf += chunk
    process.wait()
    return process.returncode == 0, "\n".join(tail_lines[-10:])


class OnboardingWindow(Adw.ApplicationWindow):
    """Setup check: Steam installed, Edge installed, SGDB key set. Shown
    whenever _all_requirements_met() fails at startup (checked live every
    launch, not cached), or reopened from the Preferences menu. Once all
    three pass, hands off to on_complete() to open the main window."""

    def __init__(self, app, on_complete, auto_advance=True):
        super().__init__(application=app, title=f"Set Up {APP_NAME}")
        self.set_default_size(700, -1)
        self.on_complete = on_complete
        # Guards against handing off twice -- the manual Continue button
        # and the background auto-advance poll are two independent event
        # sources that can both fire _on_continue right around the same
        # moment (e.g. the user clicks Continue the instant it becomes
        # sensitive, exactly when a poll tick also completes), each
        # unconditionally opening its own MainWindow. Confirmed on real
        # hardware: two main windows opened after onboarding.
        self._advanced = False
        # Auto-close and hand off the moment all 3 requirements become
        # met via the background poll (e.g. right after a real Steam
        # login completes) -- but only for the "requirements weren't
        # met yet" first-run case. Reopening via Preferences when
        # everything's already fine should never auto-close a window
        # the user just deliberately opened to look at/change something.
        self.auto_advance = auto_advance
        self.edge_ok = False
        self.sgdb_ok = False
        self._key_debounce_id = None
        self._awaiting_steam_login = False
        self.imported_count = 0

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_decoration_layout(NO_MINMAX_DECORATION_LAYOUT)
        toolbar.add_top_bar(header)

        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=24,
            margin_bottom=24,
            margin_start=24,
            margin_end=24,
        )

        group = Adw.PreferencesGroup(title="Requirements")

        self.steam_row = Adw.ActionRow(title="Steam")
        self.steam_status = Gtk.Label()
        self.steam_install_flatpak_button = Gtk.Button(label="Install Flatpak Steam", valign=Gtk.Align.CENTER)
        self.steam_install_flatpak_button.connect("clicked", self._on_install_flatpak_steam)
        self.steam_row.add_suffix(self.steam_status)
        self.steam_row.add_suffix(self.steam_install_flatpak_button)
        group.add(self.steam_row)

        self.edge_row = Adw.ActionRow(title="Microsoft Edge")
        edge_info_label = Gtk.Label(label=EDGE_REASON_TEXT, wrap=True, max_width_chars=32, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        edge_info_popover = Gtk.Popover(child=edge_info_label)
        edge_info_button = Gtk.MenuButton(
            icon_name="help-about-symbolic",
            tooltip_text="Why Edge?",
            valign=Gtk.Align.CENTER,
            popover=edge_info_popover,
        )
        self.edge_status = Gtk.Label()
        self.edge_install_button = Gtk.Button(label="Install Flatpak Microsoft Edge", valign=Gtk.Align.CENTER)
        self.edge_install_button.connect("clicked", self._on_install_edge)
        self.edge_row.add_suffix(self.edge_status)
        self.edge_row.add_suffix(self.edge_install_button)
        self.edge_row.add_suffix(edge_info_button)
        group.add(self.edge_row)

        self.key_row = Adw.PasswordEntryRow(title="SteamGridDB API key")
        self.sgdb_status = Gtk.Label()
        self.key_row.add_suffix(self.sgdb_status)
        self.key_row.connect("changed", self._on_key_changed)
        self.key_row.connect("entry-activated", self._on_key_activated)
        group.add(self.key_row)

        content.append(group)

        link = Gtk.Label(
            label=f'<a href="{SGDB_KEY_URL}">Get a free key at steamgriddb.com</a>',
            use_markup=True,
            halign=Gtk.Align.START,
        )
        content.append(link)

        self.install_progress = Gtk.ProgressBar(visible=False, show_text=True)
        content.append(self.install_progress)

        self.status_label = Gtk.Label(wrap=True)
        content.append(self.status_label)

        export_import_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
            halign=Gtk.Align.CENTER,
            margin_bottom=6,
        )
        self.export_button = Gtk.Button(label="Export Shortcuts...")
        self.export_button.connect("clicked", self._on_export)
        self.import_button = Gtk.Button(label="Import Shortcuts...", sensitive=False)
        self.import_button.connect("clicked", self._on_import)
        export_import_box.append(self.export_button)
        export_import_box.append(self.import_button)
        content.append(export_import_box)

        self.continue_button = Gtk.Button(
            label="Continue",
            css_classes=["suggested-action", "pill"],
            halign=Gtk.Align.CENTER,
            sensitive=False,
            margin_bottom=12,
        )
        self.continue_button.connect("clicked", self._on_continue)
        content.append(self.continue_button)

        # No ScrolledWindow wrapper -- this dialog never needs to scroll, and
        # ScrolledWindow's natural-size propagation to the window wasn't
        # reliably sizing the window correctly across window managers
        # (confirmed: the Continue button stayed cropped even with
        # propagate_natural_height/width set). Setting the content directly
        # lets the window's natural size genuinely reflect what it contains.
        toolbar.set_content(content)
        self.set_content(toolbar)

        self._set_status(self.sgdb_status, False)
        self._check_steam()
        self._check_edge()
        if config.get_sgdb_api_key():
            self.key_row.set_text(config.get_sgdb_api_key())
            self._check_key()

        # Installing Steam only gets you halfway there -- find_steam_root()
        # looks for a userdata dir, which Steam only creates once the user
        # actually logs in, and that login happens in Steam's own window,
        # not something Gridge gets notified about. Without re-checking
        # periodically, onboarding would keep showing "Steam not detected"
        # until the user restarted Gridge entirely even after logging in.
        self._requirements_poll_id = GLib.timeout_add_seconds(3, self._poll_requirements)
        # Covers every way this window can close (Continue, import, or just
        # the header bar's close button) in one place, rather than
        # duplicating a "stop the poll" call at each of those call sites.
        self.connect("destroy", lambda *_a: GLib.source_remove(self._requirements_poll_id))

    def _poll_requirements(self):
        self._check_steam()
        self._check_edge()
        if self.auto_advance and self.steam_ok and self.edge_ok and self.sgdb_ok:
            self._on_continue(None)
            return False
        return True

    def _set_status(self, label, ok):
        # Pango markup with an explicit color, not a themed icon + CSS class --
        # confirmed the theme-provided "success" class (and later a custom
        # CSS class targeting a Gtk.Image) didn't render as green
        # consistently across different desktop environments. Direct color
        # in the markup itself has no external theme dependency at all.
        if ok:
            label.set_markup('<span foreground="#26a269" weight="bold" size="large">✓</span>')
        else:
            label.set_markup('<span foreground="#e5a50a" weight="bold" size="large">⚠</span>')

    def _update_continue_button(self):
        all_ok = self.steam_ok and self.edge_ok and self.sgdb_ok
        self.continue_button.set_sensitive(all_ok)
        self.import_button.set_sensitive(all_ok)

    def _check_steam(self):
        try:
            root = steam_paths.find_steam_root()
            self.steam_ok = True
            self.steam_row.set_subtitle(root)
            if self._awaiting_steam_login:
                self._awaiting_steam_login = False
                self.status_label.set_label("")
        except steam_paths.SteamNotFoundError as e:
            self.steam_ok = False
            self.steam_row.set_subtitle(str(e))
            if self._awaiting_steam_login:
                # Distinguish "still downloading, hasn't even started
                # yet" from "running, just waiting on login/post-login
                # sync" -- confirmed the latter can take real, unhelped
                # time after login (Steam's own initial account sync),
                # not something Gridge's polling controls, so the
                # message shouldn't imply it's stuck.
                if steam_restart.is_steam_running():
                    self.status_label.set_label(
                        "Steam is running -- log in when its window appears. "
                        "Finishing setup after login can take a moment."
                    )
                else:
                    self.status_label.set_label(
                        "Launching Steam for first-time setup -- this can take a few "
                        "minutes, then it'll ask you to log in."
                    )
        self.steam_install_flatpak_button.set_visible(not self.steam_ok)
        self._set_status(self.steam_status, self.steam_ok)
        self._update_continue_button()

    def _on_install_flatpak_steam(self, _button):
        self.steam_install_flatpak_button.set_sensitive(False)
        self.status_label.set_label("Installing Steam from Flathub...")
        self.install_progress.set_fraction(0.0)
        self.install_progress.set_visible(True)

        def work():
            ok, err = _flatpak_install_user("com.valvesoftware.Steam", progress_callback=self._on_install_progress)
            GLib.idle_add(self._install_steam_done, ok, err)

        threading.Thread(target=work, daemon=True).start()

    def _install_steam_done(self, ok, error_output):
        self.steam_install_flatpak_button.set_sensitive(True)
        self.install_progress.set_visible(False)
        if ok:
            # The Flathub Steam package is just a small bootstrap
            # downloader -- launching it is what actually triggers the
            # real client's first-time download/install, which
            # otherwise happens completely silently for a few minutes
            # with no visible progress (confirmed: looks exactly like
            # the installer did nothing) before it ever asks to log in.
            # Detached (start_new_session=True under the hood), so it
            # keeps running even if the user closes Gridge meanwhile.
            steam_restart.launch_flatpak_steam_detached()
            self._awaiting_steam_login = True
            self._check_steam()
        else:
            self.status_label.set_label(f"Install failed: {error_output.strip()}")

    def _check_edge(self):
        try:
            exe, _ = edge_launcher.find_edge()
            self.edge_ok = True
            self.edge_row.set_subtitle(exe)
            self.edge_install_button.set_visible(False)
        except edge_launcher.EdgeNotFoundError:
            self.edge_ok = False
            self.edge_row.set_subtitle("Not installed")
            self.edge_install_button.set_visible(True)
        self._set_status(self.edge_status, self.edge_ok)
        self._update_continue_button()

    def _on_install_progress(self, fraction):
        GLib.idle_add(self.install_progress.set_fraction, fraction)

    def _on_install_edge(self, _button):
        self.edge_install_button.set_sensitive(False)
        self.status_label.set_label("Installing Microsoft Edge from Flathub...")
        self.install_progress.set_fraction(0.0)
        self.install_progress.set_visible(True)

        def work():
            ok, err = _flatpak_install_user("com.microsoft.Edge", progress_callback=self._on_install_progress)
            GLib.idle_add(self._install_edge_done, ok, err)

        threading.Thread(target=work, daemon=True).start()

    def _install_edge_done(self, ok, error_output):
        self.edge_install_button.set_sensitive(True)
        self.install_progress.set_visible(False)
        if ok:
            self.status_label.set_label("")
            self._check_edge()
        else:
            self.status_label.set_label(f"Install failed: {error_output.strip()}")

    def _on_key_changed(self, _entry):
        if self._key_debounce_id:
            GLib.source_remove(self._key_debounce_id)
        self._key_debounce_id = GLib.timeout_add(600, self._debounced_check_key)

    def _on_key_activated(self, *_args):
        if self._key_debounce_id:
            GLib.source_remove(self._key_debounce_id)
            self._key_debounce_id = None
        self._check_key()

    def _debounced_check_key(self):
        self._key_debounce_id = None
        self._check_key()
        return False

    def _check_key(self):
        key = self.key_row.get_text().strip()
        if not key:
            self.sgdb_ok = False
            self._set_status(self.sgdb_status, False)
            self.status_label.set_label("")
            self._update_continue_button()
            return
        self.status_label.set_label("Checking key...")

        def work():
            try:
                config.set_sgdb_api_key(key)
                sgdb.search("test")
            except sgdb.SGDBError as e:
                GLib.idle_add(self._key_check_failed, str(e))
                return
            GLib.idle_add(self._key_check_done)

        threading.Thread(target=work, daemon=True).start()

    def _key_check_done(self):
        self.sgdb_ok = True
        self._set_status(self.sgdb_status, True)
        self.status_label.set_label("")
        self._update_continue_button()

    def _key_check_failed(self, message):
        self.sgdb_ok = False
        self._set_status(self.sgdb_status, False)
        self.status_label.set_label(f"Error: {message}")
        self._update_continue_button()

    def _on_export(self, _button):
        dialog = Gtk.FileChooserNative(
            title="Export Shortcuts",
            transient_for=self,
            action=Gtk.FileChooserAction.SAVE,
        )
        dialog.set_current_name(f"{APP_NAME.lower()}-shortcuts-{date.today().isoformat()}.zip")
        zip_filter = Gtk.FileFilter()
        zip_filter.set_name("Zip archive")
        zip_filter.add_pattern("*.zip")
        dialog.add_filter(zip_filter)
        dialog.connect("response", self._on_export_response)
        dialog.show()

    def _on_export_response(self, dialog, response):
        if response != Gtk.ResponseType.ACCEPT:
            dialog.destroy()
            return
        path = dialog.get_file().get_path()
        dialog.destroy()
        if not path.endswith(".zip"):
            path += ".zip"

        self.export_button.set_sensitive(False)
        self.status_label.set_label("Exporting shortcuts...")

        def work():
            try:
                count = shortcuts_export.export_shortcuts(path)
                GLib.idle_add(self._export_done, count, None)
            except Exception as e:
                GLib.idle_add(self._export_done, 0, str(e))

        threading.Thread(target=work, daemon=True).start()

    def _export_done(self, count, error):
        self.export_button.set_sensitive(True)
        self.status_label.set_label("")
        if error:
            dialog = Adw.AlertDialog(heading="Export Failed", body=error)
        else:
            dialog = Adw.AlertDialog(
                heading="Export Complete",
                body=f"Exported {count} shortcut{'s' if count != 1 else ''}.",
            )
        dialog.add_response("ok", "OK")
        dialog.present(self)

    def _on_import(self, _button):
        dialog = Gtk.FileChooserNative(
            title="Import Shortcuts",
            transient_for=self,
            action=Gtk.FileChooserAction.OPEN,
        )
        zip_filter = Gtk.FileFilter()
        zip_filter.set_name("Zip archive")
        zip_filter.add_pattern("*.zip")
        dialog.add_filter(zip_filter)
        dialog.connect("response", self._on_import_response)
        dialog.show()

    def _on_import_response(self, dialog, response):
        if response != Gtk.ResponseType.ACCEPT:
            dialog.destroy()
            return
        path = dialog.get_file().get_path()
        dialog.destroy()

        self.import_button.set_sensitive(False)
        self.status_label.set_label("Importing shortcuts...")

        def work():
            try:
                count = shortcuts_export.import_shortcuts(path)
                GLib.idle_add(self._import_done, count, None)
            except Exception as e:
                GLib.idle_add(self._import_done, 0, str(e))

        threading.Thread(target=work, daemon=True).start()

    def _import_done(self, count, error):
        self._update_continue_button()
        self.status_label.set_label("")
        if error:
            dialog = Adw.AlertDialog(heading="Import Failed", body=error)
            dialog.add_response("ok", "OK")
            dialog.present(self)
            return
        self.imported_count += count
        dialog = Adw.AlertDialog(
            heading="Import Complete",
            body=f"Imported {count} shortcut{'s' if count != 1 else ''}.",
        )
        dialog.add_response("ok", "OK")
        # Import is only reachable once all 3 requirements are already
        # met (same gating as Continue), so once the user acknowledges
        # this, hand off to the main window immediately instead of
        # making them also click Continue -- it shows the same pending
        # shortcuts count there that a regular create would.
        dialog.connect("response", self._on_import_dialog_response)
        dialog.present(self)

    def _on_import_dialog_response(self, _dialog, _response):
        self._advance_to_main()

    def _on_continue(self, _button):
        self._advance_to_main()

    def _advance_to_main(self):
        if self._advanced:
            return
        self._advanced = True
        # Present the main window before closing this one, not after --
        # closing first leaves the app with zero open windows for a brief
        # moment, which is a known trigger for GNOME Shell losing track
        # of the app's dock/taskbar association entirely (confirmed on
        # real hardware: the dock icon showed correctly for onboarding
        # itself, then vanished the instant it handed off to the main
        # window). Keeping a window open throughout the handoff avoids
        # that gap.
        self.on_complete(self.imported_count)
        self.close()


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME)
        # Steam Deck's native screen resolution as the default cap --
        # bigger artwork needs more room, but this is still just the
        # default; users can resize larger or smaller themselves.
        self.set_default_size(1280, 800)

        self.match = None
        self._search_debounce_id = None
        self.pending_shortcuts_count = 0

        toolbar = Adw.ToolbarView()

        header = Adw.HeaderBar()
        header.set_decoration_layout(NO_MINMAX_DECORATION_LAYOUT)

        donate_action = Gio.SimpleAction.new("donate", None)
        donate_action.connect("activate", self._on_donate)
        self.add_action(donate_action)

        preferences_action = Gio.SimpleAction.new("preferences", None)
        preferences_action.connect("activate", self._on_preferences)
        self.add_action(preferences_action)

        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", self._on_about)
        self.add_action(about_action)

        menu = Gio.Menu()
        menu.append("Donate", "win.donate")
        menu.append("Preferences", "win.preferences")
        menu.append("About Gridge", "win.about")
        menu_button = Gtk.MenuButton(icon_name="emblem-system-symbolic", tooltip_text="Menu", menu_model=menu)
        header.pack_end(menu_button)
        toolbar.add_top_bar(header)

        # Adwaita widgets (EntryRow/PreferencesGroup) commonly default to
        # hexpand=True themselves, which cascades up through this Box
        # once it's a sibling in a horizontal layout -- without pinning
        # hexpand off and giving it back its old fixed width, it grabbed
        # far more than its original share, squeezing the artwork panel.
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=24,
            margin_bottom=24,
            margin_start=24,
            margin_end=24,
            hexpand=False,
            width_request=460,
        )

        self.url_entry = Adw.EntryRow(title="URL or service name (e.g. Netflix)")
        self.url_entry.connect("changed", self._on_url_changed)
        self.url_entry.connect("entry-activated", self._on_url_activated)
        clear_button = Gtk.Button(icon_name="edit-clear-symbolic", tooltip_text="Clear", valign=Gtk.Align.CENTER)
        clear_button.add_css_class("flat")
        clear_button.connect("clicked", self._on_clear_url)
        self.url_entry.add_suffix(clear_button)
        self.couch_mode_row = Adw.SwitchRow(
            title="Leanback mode",
            subtitle="Use YouTube's TV interface -- easier to navigate with a controller",
            visible=False,
        )
        entries_group = Adw.PreferencesGroup()
        entries_group.add(self.url_entry)
        entries_group.add(self.couch_mode_row)
        content.append(entries_group)

        self.url_hint = Gtk.Label(wrap=True, halign=Gtk.Align.START, margin_start=6)
        content.append(self.url_hint)

        self.results_group = Adw.PreferencesGroup(title="SGDB matches")

        # Lets a user search SteamGridDB directly, independent of
        # whatever the URL bar resolves to -- useful when the
        # auto-derived name guess isn't a good match, or they want to
        # link a URL to a completely different SGDB entry's artwork.
        sgdb_search_toggle = Gtk.ToggleButton(
            icon_name="system-search-symbolic",
            tooltip_text="Search SteamGridDB directly",
            valign=Gtk.Align.CENTER,
            css_classes=["flat"],
        )
        sgdb_search_toggle.connect("toggled", self._on_toggle_sgdb_search)
        self.results_group.set_header_suffix(sgdb_search_toggle)

        self.sgdb_search_entry = Gtk.SearchEntry(placeholder_text="Search SteamGridDB...", margin_bottom=10)
        self.sgdb_search_entry.connect("search-changed", self._on_sgdb_search_changed)
        self.sgdb_search_entry.connect("activate", self._on_sgdb_search_activated)
        self.sgdb_search_revealer = Gtk.Revealer(
            child=self.sgdb_search_entry,
            transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
            reveal_child=False,
        )
        self.results_group.add(self.sgdb_search_revealer)

        self.results_list = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.SINGLE)
        self.results_list.connect("row-selected", self._on_row_selected)
        self.results_list.connect("row-activated", self._on_row_activated)
        # Reserve room for ~5 rows even when empty, so the buttons/status
        # below don't jump up and down as a search starts/clears -- only
        # vexpand (not a height cap) so the list still grows if the user
        # resizes the window taller.
        results_scroller = Gtk.ScrolledWindow(
            child=self.results_list,
            vexpand=True,
            min_content_height=230,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
        )
        self.results_group.add(results_scroller)
        content.append(self.results_group)

        buttons_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.CENTER, margin_top=12
        )
        self.create_button = Gtk.Button(
            label="Create Steam Shortcut", css_classes=["suggested-action"], sensitive=False
        )
        self.create_button.connect("clicked", self._on_create)
        buttons_box.append(self.create_button)

        self.restart_steam_button = Gtk.Button(label="Restart Steam")
        self.restart_steam_button.connect("clicked", self._on_restart_steam)
        buttons_box.append(self.restart_steam_button)
        content.append(buttons_box)

        self.spinner = Gtk.Spinner()
        content.append(self.spinner)

        self.status_label = Gtk.Label(wrap=True)
        content.append(self.status_label)

        self.pending_label = Gtk.Label(wrap=True, css_classes=["dim-label"])
        content.append(self.pending_label)

        self.controller_tip_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, visible=False)
        self.controller_tip_box.append(
            Gtk.Image(icon_name="dialog-information-symbolic", valign=Gtk.Align.START, margin_top=2)
        )
        self.controller_tip_box.append(
            Gtk.Label(
                label='Remember to set Steam Input layout to "Web Browser" (or your preferred one) for each shortcut',
                wrap=True,
                # max_width_chars=1 makes Pango wrap to whatever width it's
                # actually given instead of sizing to the unwrapped text --
                # without it, this Box's natural width request stretches
                # the fixed 460px left column wider to fit the text on one
                # line (same class of hexpand fight noted above for content).
                max_width_chars=1,
                hexpand=True,
                justify=Gtk.Justification.CENTER,
                xalign=0.5,
                css_classes=["dim-label"],
            )
        )
        content.append(self.controller_tip_box)

        # The artwork panel gets its own vertical scroller -- bigger
        # artwork plus a smaller screen (the Deck's 1280x800, or any
        # maximized-but-still-small window) could otherwise push the
        # lower categories, and even the left column's Create button,
        # off-screen entirely with no way to reach them.
        artwork_scroller = Gtk.ScrolledWindow(
            child=self._build_artwork_panel(),
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            hexpand=True,
            vexpand=True,
        )

        main_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        main_box.append(content)
        main_box.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        main_box.append(artwork_scroller)

        toolbar.set_content(main_box)
        self.set_content(toolbar)

        self._last_known_size = None
        self._pending_size = None
        self._thumbnail_cache = {}
        self._clear_results()
        self._reset_artwork_panel()
        # Bigger artwork needs more room than the default size alone
        # reliably gives on a smaller screen (e.g. the Deck's 1280x800)
        # -- launch maximized so nothing's off-screen from the start;
        # users can still unmaximize/resize freely afterward.
        self.maximize()
        # A fixed-size guess can never match every real monitor (that's
        # exactly what went wrong sizing for the 1280x800 default before
        # actually seeing what "maximized" means on a real screen) --
        # poll the window's own live get_width()/get_height() instead
        # and only rebuild when they've actually changed, catching the
        # true maximized size shortly after launch and keeping it in
        # sync through any later manual resize too.
        GLib.timeout_add(400, self._refresh_for_window_size)

    def _refresh_for_window_size(self):
        size = (self.get_width(), self.get_height())

        # Debounced: only rebuild once the reported size has held
        # steady across two consecutive ticks, not on every transient
        # size seen while a maximize/resize animation is still
        # settling. Rebuilding on every intermediate size (confirmed on
        # a monitor much bigger than the 1280x800 default, where the
        # maximize animation covers a lot more distance/frames than on
        # the Deck's screen, which is already close to that default)
        # is what made this look like a slow, laggy creep instead of a
        # single snappy adjustment.
        if size != self._pending_size:
            self._pending_size = size
            return True

        if size == self._last_known_size:
            return True
        self._last_known_size = size

        panel_width = size[0] - _ARTWORK_PANEL_OVERHEAD
        for basename, _title, cell_w, _cell_h in ARTWORK_CATEGORIES:
            row = self.artwork_rows[basename]
            row["visible_count"] = max(1, (panel_width + _ARTWORK_ROW_SPACING) // (cell_w + _ARTWORK_ROW_SPACING))
            self._render_artwork_row(basename)

        if not self._showing_real_results:
            self._clear_results()

        return True

    def _build_artwork_panel(self):
        """Right-hand artwork picker: one horizontally-scrolling row per
        SGDB category, populated once a match is selected. Always
        present (not just while a match is selected) so the panel never
        pops the window's width around -- it just shows skeleton
        placeholders in its empty state. The whole panel is wrapped in
        its own vertical scroller (see caller) so bigger artwork on a
        shorter window never pushes the Create button off-screen."""
        panel = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=16,
            margin_top=24,
            margin_bottom=24,
            margin_start=16,
            margin_end=24,
            hexpand=True,
        )
        self.artwork_rows = {}
        for basename, title, cell_w, cell_h in ARTWORK_CATEGORIES:
            section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            title_box.append(Gtk.Label(label=title, halign=Gtk.Align.START, css_classes=["heading"]))
            # Spins while this category still has candidates or
            # thumbnails outstanding -- with no cap on candidate count
            # and thumbnails now loading through a bounded queue instead
            # of all at once, there's no other visible cue that a row
            # with e.g. 28 results isn't just done after the first
            # handful appear.
            spinner = Gtk.Spinner(visible=False)
            title_box.append(spinner)
            section.append(title_box)

            row_box = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL, spacing=_ARTWORK_ROW_SPACING, vexpand=False, valign=Gtk.Align.START
            )
            # ALWAYS (not AUTOMATIC) -- AUTOMATIC only reserves the
            # scrollbar's height once a category actually has more
            # results than visible_count, so a row's total height
            # (cells + scrollbar strip) differed depending on whether it
            # overflowed. The all-skeleton empty state never overflows,
            # so every category below a since-populated overflowing row
            # visibly shifted down once real results loaded (confirmed:
            # not specific to any one service, happened whenever a
            # category's result count exceeded what fit on screen).
            # ALWAYS reserves that strip unconditionally so row height
            # never depends on content. The strip itself is still hidden
            # (not removed -- opacity, so it keeps its space) whenever a
            # row genuinely has nothing to scroll, via
            # _update_row_scrollbar_visibility below -- reserving the
            # space is what fixes alignment, but a permanently-visible
            # bar under a blank/under-filled row is just noise.
            scroller = Gtk.ScrolledWindow(
                child=row_box,
                hscrollbar_policy=Gtk.PolicyType.ALWAYS,
                vscrollbar_policy=Gtk.PolicyType.NEVER,
                propagate_natural_height=True,
                vexpand=False,
            )
            hadjustment = scroller.get_hadjustment()
            hadjustment.connect("changed", self._update_row_scrollbar_visibility, scroller)
            self._update_row_scrollbar_visibility(hadjustment, scroller)
            section.append(scroller)
            panel.append(section)

            # visible_count starts as a placeholder -- a fixed estimate
            # can never be right for every real screen (confirmed:
            # maximizing on an actual monitor doesn't land on the
            # 1280x800 default this was first computed from), so the
            # real value is set by _refresh_for_window_size() once the
            # window's actual live width is known, and kept in sync as
            # it's resized/maximized afterward.
            self.artwork_rows[basename] = {
                "box": row_box,
                "cell_w": cell_w,
                "cell_h": cell_h,
                "visible_count": 1,
                "spinner": spinner,
            }

        return panel

    def _update_row_scrollbar_visibility(self, hadjustment, scroller):
        # hscrollbar_policy=ALWAYS keeps this row's scrollbar strip's
        # height reserved unconditionally (see the scroller's own
        # comment), but a strip that's always visible even for a
        # blank/under-filled row that has nothing to scroll is just
        # noise -- hide it via opacity (not remove_css_class churn on
        # every pixel of scrolling, just when whether it's needed at
        # all actually flips) whenever content doesn't overflow, while
        # its layout space stays exactly as reserved either way.
        hbar = scroller.get_hscrollbar()
        needs_scroll = hadjustment.get_upper() - hadjustment.get_lower() > hadjustment.get_page_size() + 0.5
        if needs_scroll:
            hbar.remove_css_class("artwork-row-scrollbar-hidden")
        else:
            hbar.add_css_class("artwork-row-scrollbar-hidden")

    def _reset_artwork_panel(self):
        self.artwork_candidates = {basename: [] for basename, *_ in ARTWORK_CATEGORIES}
        self.artwork_selected = {basename: None for basename, *_ in ARTWORK_CATEGORIES}
        self.artwork_pending = {basename: 0 for basename, *_ in ARTWORK_CATEGORIES}
        for basename, *_ in ARTWORK_CATEGORIES:
            spinner = self.artwork_rows[basename]["spinner"]
            spinner.stop()
            spinner.set_visible(False)
            self._render_artwork_row(basename)

    def _render_artwork_row(self, basename):
        row = self.artwork_rows[basename]
        box = row["box"]
        child = box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            box.remove(child)
            child = nxt

        candidates = self.artwork_candidates[basename]
        selected = self.artwork_selected.get(basename)
        if selected is None and candidates:
            # No explicit pick yet -- default to the first result rather
            # than requiring everyone to manually curate every category
            # just to create a shortcut. SGDB has no reliable "most
            # popular" signal to sort by instead (its score/upvote
            # fields are confirmed to always read 0 via the API), so
            # "first returned" is as sensible a default as any; clicking
            # a different one still overrides it.
            selected = candidates[0]
            self.artwork_selected[basename] = selected

        for candidate in candidates:
            is_selected = candidate is selected
            box.append(self._make_artwork_cell(basename, candidate, row["cell_w"], row["cell_h"], is_selected))

        # Top up to visible_count total cells (real + skeleton), never
        # more -- an all-skeleton row is exactly visible_count wide, so
        # it never needs to scroll, and a populated row that fits within
        # visible_count needs no extra width either, so the window
        # doesn't grow once real art replaces the placeholders. Only a
        # category with genuinely more real results than visible_count
        # ends up needing to scroll, which is the intended behavior.
        for _ in range(max(0, row["visible_count"] - len(candidates))):
            box.append(self._make_skeleton_cell(row["cell_w"], row["cell_h"]))

    def _make_skeleton_cell(self, w, h):
        # Also carries artwork-cell (not just artwork-skeleton) so it
        # reserves the exact same 3px transparent border real cells
        # always have -- without this, a skeleton cell rendered a few
        # pixels smaller than a real one, causing a visible resize the
        # moment artwork replaced it.
        #
        # hexpand/vexpand explicitly False -- width_request/height_request
        # only set a *minimum*, and on a large/4K window some ancestor
        # (the outer artwork scroller is vexpand=True to fill the window)
        # was letting these leaf widgets claim extra allocated height
        # beyond that minimum, stretching cells taller the bigger the
        # window got instead of staying a fixed size. Confirmed on a real
        # 4K display: cell width matched the request but height didn't.
        return Gtk.Box(
            css_classes=["artwork-skeleton", "artwork-cell"],
            width_request=w,
            height_request=h,
            hexpand=False,
            vexpand=False,
            halign=Gtk.Align.START,
            valign=Gtk.Align.START,
        )

    def _make_artwork_cell(self, basename, candidate, w, h, selected=False):
        # CONTAIN, not COVER -- COVER crops to fill the cell, and actual
        # artwork aspect ratios don't always match these fixed cell
        # sizes exactly (confirmed: logos/icons were visibly cropped).
        # Letterboxing inside the same fixed size keeps every cell
        # (real or skeleton) identically sized either way.
        #
        # width_request/height_request are only a *minimum* -- they don't
        # cap a widget's own natural size. hexpand/vexpand=False (added
        # previously) stops a cell from stretching to fill *leftover*
        # parent space, but a real image's aspect ratio can still make
        # Picture's own natural size, under CONTAIN, come out taller than
        # the declared height -- confirmed: rows with real artwork came
        # out taller than the all-skeleton row, and each row's extra
        # height stacked up, pushing every category title further down
        # the more rows above it had real results.
        #
        # A ScrolledWindow with propagate_natural_width/height left at
        # their GTK4 default (False) never lets its child's natural size
        # influence its own reported size at all, regardless of content --
        # a real hard clip. (Widget:overflow=HIDDEN was tried instead and
        # reverted: it only clips *painting*, it doesn't stop the parent
        # from *allocating* Picture's larger natural size in the first
        # place, so the misalignment bug would've come right back.)
        # artwork-clip strips the ScrolledWindow's own default chrome
        # (border/shadow/background) so a real cell matches a skeleton
        # cell's plain Box exactly instead of looking a hair different.
        picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, hexpand=True, vexpand=True)
        clip = Gtk.ScrolledWindow(
            child=picture,
            width_request=w,
            height_request=h,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.NEVER,
            hexpand=False,
            vexpand=False,
            css_classes=["artwork-clip"],
        )
        # artwork-skeleton (not just artwork-cell) so logos/icons with
        # transparent backgrounds get the same neutral backdrop the
        # empty-state placeholders use, instead of camouflaging into
        # whatever's behind the window (confirmed: white/dark logos were
        # nearly invisible against the app background).
        cell_classes = ["artwork-cell", "artwork-skeleton"] + (["selected"] if selected else [])
        overlay = Gtk.Overlay(
            child=clip,
            css_classes=cell_classes,
            hexpand=False,
            vexpand=False,
            halign=Gtk.Align.START,
            valign=Gtk.Align.START,
        )

        check = Gtk.Label(
            label="✓",
            css_classes=["artwork-check"],
            halign=Gtk.Align.END,
            valign=Gtk.Align.END,
            margin_end=4,
            margin_bottom=4,
            visible=selected,
        )
        overlay.add_overlay(check)

        button = Gtk.Button(
            child=overlay,
            css_classes=["flat", "artwork-button"],
            hexpand=False,
            vexpand=False,
            halign=Gtk.Align.START,
            valign=Gtk.Align.START,
        )
        button.connect("clicked", self._on_artwork_clicked, basename, candidate, overlay, check)

        self._load_thumbnail_async(basename, candidate["thumb"], picture)
        return button

    def _on_artwork_clicked(self, _button, basename, candidate, overlay, check):
        # Single-select per category: clear any previously selected cell
        # in this same row before marking the new one.
        box = self.artwork_rows[basename]["box"]
        child = box.get_first_child()
        while child:
            cell_overlay = child.get_first_child()
            if isinstance(cell_overlay, Gtk.Overlay):
                cell_overlay.remove_css_class("selected")
                cell_check = cell_overlay.get_last_child()
                if isinstance(cell_check, Gtk.Label):
                    cell_check.set_visible(False)
            child = child.get_next_sibling()

        overlay.add_css_class("selected")
        check.set_visible(True)
        self.artwork_selected[basename] = candidate

    def _load_thumbnail_async(self, basename, url, picture):
        # A window resize re-renders every row from scratch (see
        # _refresh_for_window_size), which would otherwise re-download
        # every already-loaded thumbnail each time -- caching by URL
        # means only genuinely new candidates ever hit the network.
        cached = self._thumbnail_cache.get(url)
        if cached is not None:
            picture.set_paintable(cached)
            self._thumbnail_done(basename)
            return

        def work():
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "gridge/0.1"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = resp.read()
            except Exception:
                data = None
            GLib.idle_add(self._thumbnail_loaded, basename, url, picture, data)

        # Submitted to the shared bounded executor, not a raw Thread --
        # see _THUMBNAIL_EXECUTOR's comment for why.
        _THUMBNAIL_EXECUTOR.submit(work)

    def _thumbnail_loaded(self, basename, url, picture, data):
        if data is not None:
            try:
                texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
                self._thumbnail_cache[url] = texture
                picture.set_paintable(texture)
            except GLib.Error:
                pass
        self._thumbnail_done(basename)

    def _thumbnail_done(self, basename):
        self.artwork_pending[basename] = max(0, self.artwork_pending.get(basename, 0) - 1)
        if self.artwork_pending[basename] == 0:
            spinner = self.artwork_rows[basename]["spinner"]
            spinner.stop()
            spinner.set_visible(False)

    def _fetch_artwork(self, match):
        self._reset_artwork_panel()
        game_id = match["id"]
        fetchers = {
            "grid_vertical": sgdb.get_vertical_grid_candidates,
            "grid_horizontal": sgdb.get_horizontal_grid_candidates,
            "hero": sgdb.get_hero_candidates,
            "logo": sgdb.get_logo_candidates,
            "icon": sgdb.get_icon_candidates,
        }

        # One thread per category, not one thread looping through all 5
        # sequentially -- a category's thumbnails can't start loading
        # until its own candidate list arrives, so fetching the lists
        # sequentially meant later categories (hero/logo/icon) sat
        # showing skeletons noticeably longer than they needed to,
        # queued behind earlier categories' round-trips. Each fetch now
        # starts as early as possible instead of waiting its turn.
        for basename, fetch in fetchers.items():
            spinner = self.artwork_rows[basename]["spinner"]
            spinner.set_visible(True)
            spinner.start()

            def work(basename=basename, fetch=fetch):
                try:
                    candidates = fetch(game_id)
                except Exception:
                    candidates = []
                GLib.idle_add(self._artwork_candidates_ready, match, basename, candidates)

            threading.Thread(target=work, daemon=True).start()

    def _artwork_candidates_ready(self, match, basename, candidates):
        # Guards against a race: if the user picked a different match
        # before this category's fetch finished, discard the stale
        # result instead of populating the wrong row.
        if self.match is not match:
            return
        self.artwork_pending[basename] = len(candidates)
        if not candidates:
            spinner = self.artwork_rows[basename]["spinner"]
            spinner.stop()
            spinner.set_visible(False)
        self.artwork_candidates[basename] = candidates
        self._render_artwork_row(basename)

    def _on_donate(self, _action, _param):
        Gtk.show_uri(self, DONATE_URL, 0)

    def _on_about(self, _action, _param):
        about = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon="io.github.ScarletPachydermDev.Gridge",
            version=GRIDGE_VERSION,
            developer_name="ScarletPachydermDev",
            developers=["ScarletPachydermDev"],
            website=GITHUB_URL,
            issue_url=ISSUES_URL,
            copyright="© 2026 ScarletPachydermDev",
        )
        about.present(self)

    def _on_preferences(self, _action, _param):
        def on_complete(imported_count):
            if imported_count:
                self.pending_shortcuts_count += imported_count
                self._update_pending_label()

        OnboardingWindow(self.get_application(), on_complete=on_complete, auto_advance=False).present()

    def _set_busy(self, busy, message=""):
        self.spinner.set_spinning(busy)
        self.status_label.set_label(message)
        if busy:
            self.create_button.set_sensitive(False)
        else:
            self._update_create_button()

    def _update_create_button(self):
        # A SGDB match is nice-to-have (gives real grid artwork) but not
        # required -- some services (e.g. NOW) just aren't on SGDB at
        # all, and users should still be able to create a working
        # shortcut for a valid URL/known service without one.
        resolved = resolve_url_input(self.url_entry.get_text())
        self.create_button.set_sensitive(resolved.url is not None)

    def _update_url_hint(self):
        resolved = resolve_url_input(self.url_entry.get_text())
        is_youtube = _is_plain_youtube(resolved)
        self.couch_mode_row.set_visible(is_youtube)
        if not is_youtube:
            self.couch_mode_row.set_active(False)
        if resolved.warning:
            self.url_hint.set_markup(
                f'<span foreground="#e5a50a">{GLib.markup_escape_text(resolved.warning)}</span>'
            )
        elif resolved.url:
            shown = resolved.url.removeprefix("https://").removeprefix("http://")
            self.url_hint.set_markup(
                f'<span foreground="#9a9996">Shortcut for {GLib.markup_escape_text(shown)} will be added</span>'
            )
        else:
            self.url_hint.set_label("")

    def _empty_results_list(self):
        row = self.results_list.get_row_at_index(0)
        while row:
            self.results_list.remove(row)
            row = self.results_list.get_row_at_index(0)

    def _clear_results(self):
        """Reset the results list to its empty, striped placeholder state
        rather than leaving it truly blank -- that made the reserved
        space look like a dead gray box before any search. Row count is
        computed from the window's actual live height (a fixed count
        left a visible gap of non-striped blank space once the window
        was maximized on a real, often much taller, screen)."""
        self._showing_real_results = False
        self._empty_results_list()
        available = self.get_height() - _RESULTS_LIST_CHROME_OVERHEAD
        row_count = max(5, available // _RESULTS_ROW_HEIGHT_ESTIMATE)
        for i in range(row_count):
            # Same widget type as a real match row (Adw.ActionRow), not a
            # bare Gtk.Box -- a plain box doesn't carry Adwaita's row
            # padding, so the placeholder rows came out visibly thinner
            # than actual results instead of matching their height.
            row = Adw.ActionRow(
                title="",
                selectable=False,
                activatable=False,
                css_classes=["zebra-even" if i % 2 == 0 else "zebra-odd"],
            )
            self.results_list.append(row)

    def _on_url_changed(self, _entry):
        self._update_url_hint()
        self._update_create_button()
        if self._search_debounce_id:
            GLib.source_remove(self._search_debounce_id)
        self._search_debounce_id = GLib.timeout_add(600, self._debounced_search)

    def _on_url_activated(self, *_args):
        if self._search_debounce_id:
            GLib.source_remove(self._search_debounce_id)
            self._search_debounce_id = None
        self._do_search()

    def _debounced_search(self):
        self._search_debounce_id = None
        self._do_search()
        return False

    def _do_search(self):
        self.match = None
        self.create_button.set_sensitive(False)
        self._clear_results()
        self._reset_artwork_panel()

        resolved = resolve_url_input(self.url_entry.get_text())
        if resolved.url is None:
            self._set_busy(False, "")
            return
        self._run_sgdb_search(resolved.name, resolved.sgdb_id)

    def _on_toggle_sgdb_search(self, button):
        active = button.get_active()
        self.sgdb_search_revealer.set_reveal_child(active)
        if active:
            self.sgdb_search_entry.grab_focus()
        else:
            # Just clears the field for next time -- doesn't touch
            # whatever's currently shown in the matches list, so closing
            # the search box never wipes out a selection already made.
            self.sgdb_search_entry.set_text("")

    def _on_sgdb_search_changed(self, _entry):
        self._do_sgdb_search()

    def _on_sgdb_search_activated(self, _entry):
        self._do_sgdb_search()

    def _do_sgdb_search(self):
        self.match = None
        self.create_button.set_sensitive(False)
        self._clear_results()
        self._reset_artwork_panel()

        term = self.sgdb_search_entry.get_text().strip()
        if not term:
            self._set_busy(False, "")
            return
        self._run_sgdb_search(term, None)

    def _run_sgdb_search(self, term, sgdb_id):
        self._set_busy(True, "Searching SteamGridDB...")

        def work():
            try:
                matches = [sgdb.get_game(sgdb_id)] if sgdb_id is not None else sgdb.search(term)
            except Exception as e:
                GLib.idle_add(self._search_failed, str(e))
                return
            GLib.idle_add(self._search_done, matches)

        threading.Thread(target=work, daemon=True).start()

    def _search_failed(self, message):
        self._set_busy(False, f"Error: {message}")

    def _search_done(self, matches):
        self._set_busy(False, "" if matches else "No matches found.")
        if not matches:
            self._clear_results()
            return
        self._showing_real_results = True
        self._empty_results_list()
        for i, m in enumerate(matches):
            m["name"] = cw.clean_shortcut_name(m["name"])
            row = Adw.ActionRow(
                title=m["name"], activatable=True, css_classes=["zebra-even" if i % 2 == 0 else "zebra-odd"]
            )
            row.match_data = m
            self.results_list.append(row)
        self.results_list.select_row(self.results_list.get_row_at_index(0))

    def _on_row_selected(self, _listbox, row):
        self.match = row.match_data if row else None
        self._update_create_button()
        if not self.match:
            self._reset_artwork_panel()

    def _on_row_activated(self, _listbox, row):
        # Distinct from "row-selected": this only fires on an actual
        # click/activation (even re-clicking an already-selected row),
        # never from the automatic first-row select_row() call after a
        # search -- artwork should only ever load once the user
        # deliberately picks a match, not for whatever landed on top.
        if row and row.match_data:
            self._fetch_artwork(row.match_data)

    def _on_create(self, _button):
        resolved = resolve_url_input(self.url_entry.get_text())
        if resolved.url is None:
            self.status_label.set_label("Enter a valid URL or recognized service name first.")
            return
        url = resolved.url

        match = self.match
        name = match["name"] if match else resolved.name
        # Whatever the user picked in the artwork panel (possibly
        # nothing, in every category) is what gets used -- no implicit
        # fallback to auto-picking SGDB's first result now that there's
        # a picker for it.
        selections = dict(self.artwork_selected)
        couch_mode = self.couch_mode_row.get_visible() and self.couch_mode_row.get_active()
        self._set_busy(True, f"Creating shortcut for {name}...")

        def work():
            try:
                paths = {}
                if match and any(selections.values()):
                    slug = cw.slugify(match["name"])
                    paths = cw.download_selected_assets(slug, selections)
                appid = cw.register_steam_shortcut(name, url, paths, couch_mode=couch_mode)
                GLib.idle_add(self._create_done, name, appid)
            except (steam_paths.SteamNotFoundError, edge_launcher.EdgeNotFoundError, sgdb.SGDBError) as e:
                GLib.idle_add(self._create_failed, str(e))
            except Exception as e:
                GLib.idle_add(self._create_failed, str(e))

        threading.Thread(target=work, daemon=True).start()

    def _create_done(self, name, appid):
        self._set_busy(False, f"Created '{name}' (appid {appid}).")
        self.pending_shortcuts_count += 1
        self._update_pending_label()

    def _create_failed(self, message):
        self._set_busy(False, f"Error: {message}")

    def _update_pending_label(self):
        n = self.pending_shortcuts_count
        if n == 0:
            self.pending_label.set_label("")
        else:
            self.pending_label.set_label(f"{n} shortcut{'s' if n != 1 else ''} to be added after Steam restart")
        self.controller_tip_box.set_visible(n > 0)

    def _on_clear_url(self, _button):
        self.url_entry.set_text("")
        if self._search_debounce_id:
            GLib.source_remove(self._search_debounce_id)
            self._search_debounce_id = None
        self.match = None
        self.create_button.set_sensitive(False)
        self._clear_results()
        self._reset_artwork_panel()
        self._set_busy(False, "")

    def _on_restart_steam(self, _button):
        self.restart_steam_button.set_sensitive(False)
        self.status_label.set_label("Restarting Steam...")

        def work():
            steam_restart.restart_steam()
            GLib.idle_add(self._restart_steam_done)

        threading.Thread(target=work, daemon=True).start()

    def _restart_steam_done(self):
        self.restart_steam_button.set_sensitive(True)
        self.status_label.set_label("")
        self.pending_shortcuts_count = 0
        self._update_pending_label()


class Application(Adw.Application):
    def __init__(self):
        super().__init__(application_id="io.github.ScarletPachydermDev.Gridge")
        self._css_installed = False

    def do_activate(self):
        if not self._css_installed:
            _install_status_css()
            self._css_installed = True

        win = self.props.active_window
        if win:
            win.present()
            return

        if _all_requirements_met():
            MainWindow(self).present()
        else:
            OnboardingWindow(self, on_complete=self._launch_main).present()

    def _launch_main(self, imported_count=0):
        win = MainWindow(self)
        if imported_count:
            win.pending_shortcuts_count = imported_count
            win._update_pending_label()
        win.present()


def main():
    Application().run()


if __name__ == "__main__":
    main()
