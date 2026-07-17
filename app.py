import app
import asyncio
import neopixel

from app_components import Menu, Notification, clear_background, layout
from app_components.utils import path_isfile
from events.input import BUTTON_TYPES, ButtonDownEvent
from firmware_apps.settings_app import PAT_DIR, SettingsApp
from system.eventbus import eventbus
from system.capabilities.utils import get_running_apps_by_capability
from system.hexpansion.events import (
    HexpansionMountedEvent,
    HexpansionUnmountedEvent,
)
from system.patterndisplay.events import PatternEnable, PatternDisable
from tildagonos import tildagonos
from patterns.rainbow import RainbowPattern
from patterns.cylon import CylonPattern
from patterns.flash import FlashPattern
from patterns.off import OffPattern

NEOPIXELS_CAPABILITY = (
    "https://tildagon.badge.emfcamp.org/capabilities/registry/neopixels/"
)
MERGED_NEOPIXELS_CAPABILITY = (
    "https://tildagon.badge.emfcamp.org/capabilities/registry/merged_neopixels/"
)

# "Create pattern" is hidden for now; it will return later.
main_menu_items = ["Choose LEDs", "Patterns"]

# The patterns built into the firmware, named and ordered as the settings app
# lists them. Everything else is a user pattern installed under PAT_DIR.
builtin_pattern_names = ["rainbow", "cylon", "flash", "off"]
builtin_pattern_classes = {
    "rainbow": RainbowPattern,
    "cylon": CylonPattern,
    "flash": FlashPattern,
    "off": OffPattern,
}

led_orderings = ["Sequential", "Parallel"]

# The front ring exposes 12 logical LEDs (physical 1..12 of tildagonos.leds;
# physical 0 and 13..18 are the back LEDs, which are driven elsewhere).
FRONT_LED_COUNT = 12
FRONT_LED_OFFSET = 1

HEXPANSION_PORTS = (1, 2, 3, 4, 5, 6)
# Sources we cannot place on the ring sort after the ones we can.
UNKNOWN_PORT = 7

# The ring is a circle: LED 0 sits just before the port 1 hexpansion and LED 1
# just after, LED 2 before port 2 and LED 3 after, and so on until LED 11, which
# closes the loop back round to LED 0. Splitting the ring at every port leaves
# these runs of adjacent LEDs, listed from LED 0 round to LED 11. Port N sits in
# the gap after run N-1, so there is one more run than there are ports: the last
# gap is the seam where LED 11 meets LED 0 again.
FRONT_RUNS = [[0], [1, 2], [3, 4], [5, 6], [7, 8], [9, 10], [11]]


def on_off(value):
    return "Yes" if value else "No"


def load_patterns():
    # Discover patterns the way the settings app does, so this menu offers
    # exactly what the badge's own pattern setting offers: the built-in ones,
    # plus any the user has installed under PAT_DIR. Each entry is a
    # (display name, directory) pair, with a directory of None meaning built in.
    # load_options never touches its instance, so it can be borrowed unbound
    # rather than standing up a second SettingsApp.
    patterns = [(name, None) for name in builtin_pattern_names]
    return SettingsApp.load_options(None, PAT_DIR, patterns)


def load_pattern_class(name, directory):
    # Built-in patterns are imported directly. User patterns are loaded the way
    # the firmware's PatternDisplay loads them, from pattern.<directory>.app
    # with the class exported as __pattern_export__.
    if directory is None:
        return builtin_pattern_classes[name]
    path = "{}/{}/app.py".format(PAT_DIR, directory)
    if not path_isfile(path):
        raise ValueError("No pattern app at {}".format(path))
    module = __import__(
        "pattern.{}.app".format(directory),
        globals(),
        locals(),
        ["__pattern_export__"],
    )
    return getattr(module, "__pattern_export__")


def app_port(led_app):
    # The hexpansion port a source is plugged into, or None if it does not
    # report one we recognise.
    try:
        port = led_app.config.port
    except Exception:
        return None
    return port if port in HEXPANSION_PORTS else None


def app_name(led_app):
    name = getattr(led_app, "name", None) or type(led_app).__name__
    try:
        return "{} (port {})".format(name, led_app.config.port)
    except Exception:
        return name


class AdvancedLEDs(app.App):
    def __init__(self):
        self.front_control = False
        self.hexpansion_control = [False, False, False, False, False, False, ]
        self.pattern = None
        # The selected pattern class, re-instantiated to fit the combined ring
        # whenever its length changes.
        self.pattern_class = RainbowPattern
        self.notification = None
        self.current_menu = "main"
        self.menu = None
        # (display name, directory) pairs, rebuilt whenever the menu is opened.
        self.patterns = []
        self.leds_layout = None
        # Selected LED grouping for each merged-neopixel app, keyed by instance.
        self.led_app_group = {}
        # How the enabled sources are laid out into one combined string.
        self.led_ordering = "Sequential"
        # The combined string driven by the current pattern, rebuilt on change.
        self.combined_leds = None
        # Number of logical LEDs in the combined string (the front ring counts
        # as 12, ignoring its unused trailing LEDs).
        self.combined_length = 0
        self._rebuild_composed()
        self.set_menu("main")
        eventbus.on_async(ButtonDownEvent, self._leds_button_handler, self)
        # Rebuild the LED list whenever hexpansions come and go.
        eventbus.on_async(HexpansionMountedEvent, self._hexpansion_changed, self)
        eventbus.on_async(HexpansionUnmountedEvent, self._hexpansion_changed, self)

    def set_menu(self, menu_name):
        if self.menu is not None:
            self.menu._cleanup()
        self.current_menu = menu_name
        if menu_name == "main":
            items = main_menu_items
        elif menu_name == "Patterns":
            # Re-scan on every visit, so a pattern installed since the app
            # started still shows up.
            self.patterns = load_patterns()
            items = [name for (name, _) in self.patterns]
        self.menu = Menu(
            self,
            items,
            select_handler=self.select_handler,
            back_handler=self.back_handler,
        )

    def select_handler(self, item, idx):
        if self.current_menu == "main":
            if item == "Choose LEDs":
                self.open_leds()
            elif item == "Patterns":
                self.set_menu("Patterns")
            else:
                self.notification = Notification('You selected "' + item + '"!')
        elif self.current_menu == "Patterns":
            name, directory = self.patterns[idx]
            try:
                self.pattern_class = load_pattern_class(name, directory)
            except Exception as e:
                # A user pattern can be missing or broken; leave the running
                # pattern alone rather than dropping the ring to nothing.
                print("Could not load pattern %s: %s" % (name, e))
                self.notification = Notification('"' + name + '" failed to load')
                self.set_menu("main")
                return
            # Rebuild the combined string and size the pattern to fit it.
            self._rebuild_composed()
            self.notification = Notification('Pattern set to "' + name + '"!')
            self.set_menu("main")

    def back_handler(self):
        if self.current_menu == "main":
            self.minimise()
        else:
            self.set_menu("main")

    def open_leds(self):
        # Swap out the menu (which owns the buttons) for the settings layout.
        if self.menu is not None:
            self.menu._cleanup()
            self.menu = None
        self.current_menu = "leds"
        self.build_leds_layout()

    def close_leds(self):
        self.leds_layout = None
        self.set_menu("main")

    def build_leds_layout(self):
        items = []
        # First item: the badge's own front board LEDs.
        self._add_toggle_row(
            items,
            "Front board LEDs",
            lambda: self.front_control,
            self._set_front_control,
        )
        # One item per running app that provides the neopixels capability.
        merged_apps = set(
            get_running_apps_by_capability(MERGED_NEOPIXELS_CAPABILITY)
        )
        for led_app in get_running_apps_by_capability(NEOPIXELS_CAPABILITY):
            self._add_source_row(items, led_app)
            # If the app also groups its neopixels, let the user cycle through
            # the groupings it defines.
            if led_app in merged_apps:
                self._add_group_row(items, led_app)
        # How the enabled sources are combined into one string.
        self._add_ordering_row(items)
        self.leds_layout = layout.LinearLayout(items=items)

    def _front_string(self, leds):
        # A substring of the front ring, given logical LED numbers. Merging
        # rather than composing keeps the length honest: the substring reports
        # exactly the LEDs asked for, so it can be placed at an offset without
        # the rest of tildagonos.leds colliding with the next source along.
        return neopixel.MergedNeoPixel(
            tildagonos.leds, [[led + FRONT_LED_OFFSET] for led in leds]
        )

    def _enabled_sources(self):
        # Every source we hold the lease on, ordered by port so the string
        # follows the physical layout of the badge rather than the order the
        # apps happened to start in.
        led_apps = [
            led_app
            for led_app in get_running_apps_by_capability(NEOPIXELS_CAPABILITY)
            if getattr(led_app, "led_owner", None) is self
            and getattr(led_app, "leds", None) is not None
        ]
        led_apps.sort(key=lambda led_app: app_port(led_app) or UNKNOWN_PORT)
        return [
            (app_port(led_app), led_app.leds, led_app.leds.n) for led_app in led_apps
        ]

    def _circular_sources(self, sources):
        # Lay the string out as the LEDs physically sit on the badge: the front
        # runs in order, with each port's LEDs interposed into the gap that port
        # occupies. A port with nothing enabled leaves its gap closed, so its
        # neighbouring runs simply join up.
        by_port = {}
        unplaceable = []
        for port, string, length in sources:
            if port is None:
                unplaceable.append((string, length))
            else:
                by_port.setdefault(port, []).append((string, length))

        parts = []
        for idx, run in enumerate(FRONT_RUNS):
            parts.append((self._front_string(run), len(run)))
            if idx < len(HEXPANSION_PORTS):
                parts.extend(by_port.get(HEXPANSION_PORTS[idx], []))
        # Sources with no port to sit beside hang off the end of the circle.
        parts.extend(unplaceable)
        return parts

    def _rebuild_composed(self):
        # Combine every enabled source into a single string. "Sequential" walks
        # the badge's own geometry, interposing each hexpansion into the gap it
        # sits in; "Parallel" overlays every source at offset zero.
        sources = self._enabled_sources()
        if self.led_ordering == "Sequential" and self.front_control:
            parts = self._circular_sources(sources)
        else:
            # Without the front ring there are no gaps to interpose into, so the
            # hexpansions just run end to end in port order.
            parts = [(string, length) for (_, string, length) in sources]
            if self.front_control:
                parts.append(
                    (self._front_string(range(FRONT_LED_COUNT)), FRONT_LED_COUNT)
                )

        composed = None
        offset = 0
        total = 0
        for string, length in parts:
            if composed is None:
                composed = neopixel.ComposedNeoPixel(string, offset)
            else:
                composed.add_string(string, offset)
            if self.led_ordering == "Sequential":
                offset += length
                total += length
            else:
                # Parallel: everything overlaps, so the ring is as long as the
                # longest source.
                total = max(total, length)

        self.combined_leds = composed
        self.combined_length = total
        self._resize_pattern()

    def _resize_pattern(self):
        # (Re)build the pattern instance so its frames match the current ring
        # length. Some patterns take the LED count, some do not.
        if self.pattern_class is None or not self.combined_length:
            self.pattern = None
            return
        try:
            self.pattern = self.pattern_class(self.combined_length)
        except TypeError:
            self.pattern = self.pattern_class()

    async def background_task(self):
        # Drive the combined ring from the current pattern, the same way the
        # firmware pattern app drives the front ring.
        while True:
            if self.pattern and self.combined_leds is not None:
                try:
                    next_frame = self.pattern.next()
                    count = min(len(next_frame), self.combined_length)
                    for led in range(count):
                        self.combined_leds[led] = next_frame[led]
                    self.combined_leds.write()
                    if not self.pattern.fps:
                        break
                    await asyncio.sleep(1 / self.pattern.fps)
                except Exception as e:
                    print("Pattern crashed: %s" % e)
                    self.pattern = None
                    await asyncio.sleep(1)
            else:
                await asyncio.sleep(1)

    def _set_front_control(self, value):
        self.front_control = value
        # Taking direct control means the background pattern engine must let go
        # of the front ring; releasing it hands the ring back.
        if value:
            eventbus.emit(PatternDisable())
        else:
            eventbus.emit(PatternEnable())
        self._rebuild_composed()

    def _add_toggle_row(self, items, label, get_state, set_state):
        display = layout.DefinitionDisplay(label, on_off(get_state()))

        async def _toggle(event):
            if BUTTON_TYPES["CONFIRM"] in event.button:
                set_state(not get_state())
                display.value = on_off(get_state())
                return True
            return False

        items.append(display)
        items.append(layout.ButtonDisplay("Toggle", button_handler=_toggle))

    def _add_source_row(self, items, led_app):
        name = app_name(led_app)

        def state_label():
            owner = getattr(led_app, "led_owner", None)
            if owner is self:
                return "Yes"
            if owner is None:
                return "No"
            return "In use"

        display = layout.DefinitionDisplay(name, state_label())

        async def _toggle(event):
            if BUTTON_TYPES["CONFIRM"] in event.button:
                owner = getattr(led_app, "led_owner", None)
                if owner is None:
                    # The source is free: claim it for us.
                    led_app.led_owner = self
                    display.value = state_label()
                    self._rebuild_composed()
                elif owner is self:
                    # We hold the lease: release it so others can take over.
                    led_app.led_owner = None
                    display.value = state_label()
                    self._rebuild_composed()
                else:
                    self.notification = Notification(
                        '"' + name + '" is in use by another app'
                    )
                return True
            return False

        items.append(display)
        items.append(layout.ButtonDisplay("Toggle", button_handler=_toggle))

    def _add_group_row(self, items, led_app):
        groups = list(led_app.LED_GROUPS.keys())
        if not groups:
            return
        if self.led_app_group.get(led_app) not in groups:
            self.led_app_group[led_app] = groups[0]
        display = layout.DefinitionDisplay(
            app_name(led_app) + " grouping", self.led_app_group[led_app]
        )

        async def _next(event):
            if BUTTON_TYPES["CONFIRM"] in event.button:
                # Only touch the grouping while we hold the lease on this source.
                if getattr(led_app, "led_owner", None) is not self:
                    self.notification = Notification(
                        '"' + app_name(led_app) + '" is not enabled'
                    )
                    return True
                idx = (groups.index(self.led_app_group[led_app]) + 1) % len(groups)
                name = groups[idx]
                self.led_app_group[led_app] = name
                try:
                    led_app.setup_led_group(name)
                except Exception:
                    pass
                display.value = name
                # setup_led_group replaces the app's leds string, so the
                # combined string must be rebuilt to point at the new one.
                self._rebuild_composed()
                return True
            return False

        items.append(display)
        items.append(layout.ButtonDisplay("Next", button_handler=_next))

    def _add_ordering_row(self, items):
        display = layout.DefinitionDisplay("LED ordering", self.led_ordering)

        async def _next(event):
            if BUTTON_TYPES["CONFIRM"] in event.button:
                idx = (led_orderings.index(self.led_ordering) + 1) % len(
                    led_orderings
                )
                self.led_ordering = led_orderings[idx]
                display.value = self.led_ordering
                # The offsets depend on the ordering, so rebuild the string.
                self._rebuild_composed()
                return True
            return False

        items.append(display)
        items.append(layout.ButtonDisplay("Next", button_handler=_next))

    async def _hexpansion_changed(self, event):
        # A hexpansion coming or going invalidates the driven array: a removed
        # source must stop being written, and a re-enumerated one comes back as
        # a fresh instance we no longer own. Rebuild so we only ever drive
        # sources we currently hold the lease on.
        self._rebuild_composed()
        # The list is only visible on the LED screen; elsewhere it is rebuilt
        # from scratch when reopened.
        if self.current_menu == "leds":
            self.build_leds_layout()

    async def _leds_button_handler(self, event):
        if self.current_menu != "leds":
            return
        handled = await self.leds_layout.button_event(event)
        if not handled and BUTTON_TYPES["CANCEL"] in event.button:
            self.close_leds()

    def update(self, delta):
        if self.current_menu != "leds" and self.menu is not None:
            self.menu.update(delta)
        if self.notification:
            self.notification.update(delta)

    def draw(self, ctx):
        clear_background(ctx)
        if self.current_menu == "leds":
            self.leds_layout.draw(ctx)
        else:
            self.menu.draw(ctx)
        if self.notification:
            self.notification.draw(ctx)


__app_export__ = AdvancedLEDs
