# Rehome: thin wrappers around packaging/layout.py, the single source of truth
# for the installation layout (shared with install.sh and the distro packages).
#
#   make install   PREFIX=/usr/local DESTDIR=
#   make uninstall PREFIX=/usr/local
#   make manifest  PREFIX=/usr/local      # list of installed paths
#   make check                            # unit tests
#   make deb | rpm | srcinfo | dist       # packages (see docs/PACKAGING.md)
#
# polkit only reads actions from /usr/share/polkit-1/actions, so the policy
# goes there regardless of PREFIX.

PREFIX     ?= /usr/local
DESTDIR    ?=
PYTHON     ?= /usr/bin/python3
LIBEXECDIR ?= $(PREFIX)/libexec/rehome

LAYOUT     := $(PYTHON) packaging/layout.py
LAYOUT_ARGS = --prefix "$(PREFIX)" --libexecdir "$(LIBEXECDIR)"
VERSION    := $(shell $(LAYOUT) version)

.PHONY: all install uninstall manifest check clean deb rpm srcinfo dist

all:
	@echo "Rehome $(VERSION): nothing to build. Use 'make install'."

install:
	$(LAYOUT) install $(LAYOUT_ARGS) --python "$(PYTHON)" --destdir "$(DESTDIR)" --quiet

uninstall:
	$(LAYOUT) uninstall $(LAYOUT_ARGS) --destdir "$(DESTDIR)"

manifest:
	@$(LAYOUT) list $(LAYOUT_ARGS) --python "$(PYTHON)"

check:
	$(PYTHON) -m pytest -q tests/unit

deb:
	packaging/deb/build.sh

rpm:
	packaging/rpm/build.sh

srcinfo:
	cd packaging/arch && makepkg --printsrcinfo > .SRCINFO

dist:
	packaging/dist.sh

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf dist build
