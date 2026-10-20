"""Drive the Rehome GUI on a virtual display: run a scenario, save window snapshots as PNG.

A scenario module defines steps(ctx, snap, find, find_all) yielding (delay_ms, callable).
Run it through tour.sh.
"""
import os, sys, traceback, importlib.util
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk, Graphene

from rehome.ui.app import RehomeApp

OUT = os.environ.get("SHOTS", "shots")
os.makedirs(OUT, exist_ok=True)


def snap(widget, name):
    """Render a widget (normally a toplevel window) to OUT/name.png."""
    try:
        w, h = widget.get_width(), widget.get_height()
        if w <= 0 or h <= 0:
            print("SNAP-SKIP", name, w, h); return
        paintable = Gtk.WidgetPaintable.new(widget)
        s = Gtk.Snapshot()
        paintable.snapshot(s, w, h)
        node = s.to_node()
        if node is None:
            print("SNAP-EMPTY", name); return
        renderer = widget.get_native().get_renderer()
        tex = renderer.render_texture(node, Graphene.Rect().init(0, 0, w, h))
        tex.save_to_png(os.path.join(OUT, name + ".png"))
        print("SNAP", name, w, h, flush=True)
    except Exception:
        traceback.print_exc()


def find(widget, pred):
    """Depth-first search of the widget tree."""
    if pred(widget):
        return widget
    child = widget.get_first_child()
    while child is not None:
        r = find(child, pred)
        if r is not None:
            return r
        child = child.get_next_sibling()
    return None


def find_all(widget, pred, acc=None):
    acc = [] if acc is None else acc
    if pred(widget):
        acc.append(widget)
    child = widget.get_first_child()
    while child is not None:
        find_all(child, pred, acc)
        child = child.get_next_sibling()
    return acc


def label_text(w):
    return w.get_label() if isinstance(w, Gtk.Label) else None


def windows(app):
    return [w for w in Gtk.Window.list_toplevels() if w.get_visible()]


class Ctx:
    def __init__(self, app):
        self.app = app

    @property
    def win(self):
        return self.app.window


def main():
    scen_path = sys.argv[1]
    spec = importlib.util.spec_from_file_location("scenario", scen_path)
    scen = importlib.util.module_from_spec(spec); spec.loader.exec_module(scen)
    app = RehomeApp()
    ctx = Ctx(app)
    steps = list(scen.steps(ctx, snap, find, find_all))

    def run_next():
        if not steps:
            print("DONE", flush=True)
            app.quit_app()
            return False
        delay, fn = steps.pop(0)
        def go():
            try:
                fn()
            except Exception:
                traceback.print_exc()
            GLib.timeout_add(10, run_next)
            return False
        GLib.timeout_add(delay, go)
        return False

    app.connect("activate", lambda *_: GLib.timeout_add(1500, run_next))
    GLib.timeout_add_seconds(int(os.environ.get("GUI_TIMEOUT", "240")), lambda: (print("TIMEOUT"), os._exit(3)))
    sys.exit(app.run(["rehome"]))


main()
