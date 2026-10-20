"""Default tour: main window, the first VM's tabs, wizards and dialogs, dark mode."""

from gi.repository import Adw, Gtk


def steps(ctx, snap, find, find_all):
    def top(cls_name):
        return next((w for w in Gtk.Window.list_toplevels()
                     if type(w).__name__ == cls_name and w.get_visible()), None)

    yield 500, lambda: snap(ctx.win, "01-main")

    def first_vm():
        rows = list(ctx.win.rows.values())
        if rows:
            ctx.win.listbox.select_row(rows[0])
    yield 0, first_vm
    yield 1500, lambda: snap(ctx.win, "02-vm-summary")
    for tab in ("checkpoints", "settings", "logs"):
        def show(t=tab):
            vm_id = ctx.win.selected_id()
            if vm_id:
                ctx.win.pages[vm_id].stack.set_visible_child_name(t)
        yield 0, show
        yield 1000, (lambda t=tab: snap(ctx.win, "03-vm-" + t))
    yield 0, lambda: ctx.app.activate_action("new-vm", None)
    yield 1200, lambda: snap(top("NewVmWizard"), "10-new-vm")
    yield 0, lambda: top("NewVmWizard").src_catalog.set_active(True)
    yield 800, lambda: snap(top("NewVmWizard"), "11-quick-create")
    yield 0, lambda: top("NewVmWizard").close()
    yield 300, lambda: ctx.app.activate_action("import", None)
    yield 2000, lambda: snap(top("ImportWizard"), "20-import")
    yield 0, lambda: top("ImportWizard").close()
    yield 300, lambda: ctx.app.activate_action("doctor", None)
    yield 4000, lambda: snap(top("DoctorWindow"), "30-host-check")
    yield 0, lambda: top("DoctorWindow").close()
    yield 300, lambda: ctx.app.activate_action("preferences", None)
    yield 1000, lambda: snap(top("PreferencesWindow"), "31-preferences")
    yield 0, lambda: top("PreferencesWindow").close()
    yield 300, lambda: ctx.win.activate_action("win.show-help-overlay", None)
    yield 1000, lambda: snap(top("ShortcutsWindow"), "32-shortcuts")
    yield 0, lambda: top("ShortcutsWindow").close()
    yield 300, lambda: Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
    yield 1500, lambda: snap(ctx.win, "40-main-dark")
