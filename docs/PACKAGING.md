# Installing and packaging Rehome

Rehome can be installed four ways. All four put the same files in the same
places, because they all call one script, `packaging/layout.py`:

| Method | Command | Prefix | Helper (`LIBEXECDIR`) |
|---|---|---|---|
| Install script | `curl -fsSL https://raw.githubusercontent.com/oreo1298/Disk-to-VM-Manager/main/install.sh \| bash` | `/usr/local` | `/usr/local/libexec/rehome` |
| Arch (AUR) | `makepkg -si` in `packaging/arch` | `/usr` | `/usr/lib/rehome` |
| Debian/Ubuntu | `packaging/deb/build.sh`, then `sudo apt install ./dist/rehome_*_all.deb` | `/usr` | `/usr/libexec/rehome` |
| Fedora/openSUSE | `packaging/rpm/build.sh [--suse]`, then `sudo dnf install ./dist/rehome-*.noarch.rpm` | `/usr` | `%{_libexecdir}/rehome` |
| From a checkout | `sudo make install` (wraps `layout.py install`) | `/usr/local` | `/usr/local/libexec/rehome` |

All runtime dependencies are distribution packages. They are listed once, in
`data/packages.toml`, per distribution family. `rehome doctor`, `install.sh` and
the packages all read that file, and unit tests check that the PKGBUILD and the
RPM spec match it.

## Installation layout

| Path | Content |
|---|---|
| `PREFIX/lib/rehome/rehome/` | The Python package: every file under `rehome/` except `__pycache__`, hidden and editor files, and the helper script |
| `PREFIX/bin/rehome` | A generated launcher (see below) |
| `PREFIX/share/rehome/` | `data/` (profiles, `packages.toml`, …), except templates (`*.in`), the desktop file, the metainfo and the icons |
| `LIBEXECDIR/rehome-helper` | The polkit helper, with the shebang rewritten to `#!PYTHON -I` |
| `/usr/share/polkit-1/actions/io.github.oreo1298.Rehome.policy` | The polkit policy, with `@HELPER@` replaced by the helper's path. polkit only reads this directory, so it is used whatever the prefix. |
| `PREFIX/share/applications/io.github.oreo1298.Rehome.desktop` | The desktop entry, with `@BINDIR@` replaced |
| `PREFIX/share/metainfo/io.github.oreo1298.Rehome.metainfo.xml` | AppStream metadata |
| `PREFIX/share/icons/hicolor/{scalable,symbolic}/apps/` | The app icon and its symbolic variant |
| `PREFIX/share/rehome/install-manifest.json` | `install.sh` only: the record of everything it changed |
| `PREFIX/share/rehome/installer/{install.sh,layout.py}` | `install.sh` only: so `--update` and `--uninstall` work offline |

`rehome doctor` (`rehome/host/tools.py`) looks for the helper at all three
`LIBEXECDIR`s above. The launcher also passes the exact path in
`REHOME_HELPER_PATH`.

The launcher puts `PREFIX/lib/rehome` first on `sys.path`. It sets
`REHOME_DATA_DIR` and `REHOME_HELPER_PATH`, and it sets `sys.pycache_prefix` to
`~/.cache/rehome/pycache` (or under `$XDG_CACHE_HOME`). Because bytecode never
goes next to the installed sources, running `sudo rehome …` cannot leave
`__pycache__` directories behind: the package manager or the uninstaller always
removes exactly what it installed. The packages therefore ship no `.pyc` files.

### layout.py

```
packaging/layout.py install   [--prefix P] [--destdir D] [--libexecdir L] [--python PY]
                              [--installer-copy] [--manifest [--manifest-data JSON]] [--quiet]
packaging/layout.py uninstall [--prefix P] [--destdir D] [--libexecdir L] | --manifest FILE
packaging/layout.py list      [--prefix P] [--libexecdir L] [--installer-copy] [--json]
packaging/layout.py version
packaging/layout.py deps --family arch|debian|fedora|suse [--required|--optional|--all]
                         [--format lines|words|deb|tsv|json]
```

- **`install`**
  - Writes each file atomically (temp file plus rename), so a running Rehome or
    installer never reads a half-written file.
  - Sets the modes explicitly (0644/0755, and 0755 for directories), whatever the
    umask.
  - Prints the installed paths.
- **`install --manifest`** additionally:
  - removes files that the previous manifest listed but this version no longer
    ships;
  - records the directories it created, so that uninstall can remove them;
  - writes the manifest.
- **`uninstall --manifest`**
  - Removes exactly the listed files, and only inside Rehome's own locations:
    `lib/rehome`, `share/rehome`, `libexec/rehome` and the six single files. A
    tampered manifest cannot make it delete anything else.
  - Then removes empty directories.
- **`uninstall` without a manifest** (what `make uninstall` runs) removes the
  layout's files plus anything else inside Rehome's own directories.

## install.sh

```
curl -fsSL https://raw.githubusercontent.com/oreo1298/Disk-to-VM-Manager/main/install.sh | bash
curl -fsSL …/install.sh | bash -s -- --update
./install.sh --help
```

| Option | Meaning |
|---|---|
| `--update` | Get the latest source and reinstall it, keeping VMs and settings. Says "already up to date" when the version matches the manifest. Refuses older versions unless `--force` is given. |
| `--uninstall` | Reverse everything the manifest records. Keeps `~/.local/share/rehome`, `~/.config/rehome`, `~/.cache/rehome` and `~/.local/state/rehome`, and says where they are. |
| `--purge` | `--uninstall`, then delete the invoking user's Rehome data after you type `delete` (`--yes` accepts). It refuses while Rehome VMs are running. Without an installation it deletes only the data. |
| `-y`, `--yes` | Accept every confirmation (for scripts). |
| `-n`, `--dry-run` | Only print what would be done. It may still download the source into a temporary directory and query the package database. |
| `--with-optional` / `--no-optional` | Install, or do not offer, the optional components. |
| `--skip-deps` | Install only Rehome's own files, no distribution packages. |
| `--keep-deps` | With `--uninstall`/`--purge`: keep the packages the installer added. |
| `--no-kvm-group`, `--no-kernel-fix` | Do not offer these two system changes. |
| `--prefix DIR` | Default `/usr/local`. |
| `--source SRC` | Same as `REHOME_SOURCE`. |
| `--detect` | Print the detected distribution and package manager. |

### Safety rule: every root command is shown first

Every command that needs root goes through one function, `priv`. It:

1. prints the reason ("Needs root: …") and the exact command line, including
   `sudo`;
2. asks `Run this command? [Y/n]` (`--yes` answers yes and says so in the
   output; `--dry-run` stops at this step);
3. only then runs the command.

The prompts read from `/dev/tty`, so they also work with `curl | bash`, where
standard input is the script itself. For the same reason the whole script is
one `main` function called on its last line, and a truncated download runs
nothing.

Without a terminal and without `--yes`, the installer stops instead of
guessing. End of input (Ctrl-D) is never taken as consent.

When run as root, the commands run without `sudo` and the invoking user is
`$SUDO_USER`. The container tests replace `sudo` with a logging shim and check
that every command sudo ran had been printed first, with a reason.

### Steps

1. **Distribution.** The installer reads `ID` and `ID_LIKE` from
   `/etc/os-release` without sourcing it:

   | Family | Package manager | IDs |
   |---|---|---|
   | `arch` | pacman | arch, archarm, manjaro, endeavouros, garuda, artix, cachyos, arcolinux |
   | `debian` | apt-get | debian, ubuntu, linuxmint, pop, elementary, zorin, kali, raspbian, neon, devuan, mx, deepin, parrot |
   | `fedora` | dnf (or dnf5) | fedora, rhel, centos, rocky, almalinux, nobara, ultramarine |
   | `suse` | zypper | suse, opensuse, opensuse-*, sles, sled, sle-micro |

   On an unknown distribution, or when the family's package manager is missing
   (for example on an immutable system), the installer prints the required
   components with their Arch, Debian, Fedora and openSUSE package names and
   asks you to install them, then run again with `--skip-deps`.

2. **Python.** The installer needs Python 3.11 or newer (for `tomllib`). It
   prefers `/usr/bin/python3`, so the launcher uses the interpreter that has
   PyGObject. If Python is missing it installs `python3` (`python` on Arch)
   first. If the system's Python is too old, it stops and explains why.

3. **Source.** The first of these that applies:
   - `$REHOME_SOURCE` or `--source`: a directory, a `.tar.gz` (verified against
     `FILE.sha256` if one sits next to it) or an `http(s)://`/`file://` URL
     (verified against `URL.sha256` if the server has one).
   - The git checkout the script runs from.
   - The latest GitHub release, from `releases/latest` in the GitHub API:
     - If the release has a `rehome-*.tar.gz` asset with a
       `rehome-*.tar.gz.sha256` (or `SHA256SUMS`) asset, it downloads both and
       verifies the checksum. A mismatch aborts before anything is installed.
     - Otherwise it uses GitHub's tag archive, which has no checksum, and says
       so.
     - With no release published, it uses the `main` branch tarball and warns
       about it.

4. **Packages.**
   - Reads `data/packages.toml` through `layout.py deps`, which uses
     `tomllib`.
   - Installs the `required = true` components, and the optional ones if
     `--with-optional` is given or you choose them at the prompt (`a`, `n`, or
     numbers; the default is none).
   - Records which packages were not installed before it ran. The check uses
     `dpkg-query` on Debian, `pacman -T` on Arch, and `rpm -q --whatprovides`
     on Fedora and openSUSE.
   - Tries all the missing packages in one command first. If that fails
     (typically because one name does not exist on this release), it tries
     them one at a time and reports exactly which names failed.
   - Rehome itself is still installed afterwards; `rehome doctor` explains
     what is missing.
   - On Arch it also points out that "target not found" usually means the
     system needs `pacman -Syu`. It never runs `pacman -Sy` itself, because
     that would be a partial upgrade.

   The commands it runs, and on apt the extra steps:

   | Family | Install command |
   |---|---|
   | Arch | `pacman -S --needed --noconfirm` |
   | Debian | `apt-get install -y`, with `DEBIAN_FRONTEND=noninteractive` when there is no terminal |
   | Fedora | `dnf install -y` |
   | openSUSE | `zypper --non-interactive install` |

   On apt it runs `apt-get update` once first.

5. **kvm group.** If a `kvm` group exists and the invoking user (not root) is
   not in it, the installer offers `usermod -aG kvm USER` (default yes) and
   records it.

6. **Kernel image (Debian/Ubuntu).**
   - libguestfs builds its appliance from the host kernel, which Ubuntu
     installs as mode 0600. `rehome doctor` (`check_appliance_kernel`) reports
     this and recommends `dpkg-statoverride`.
   - For the running kernel, and for the newest kernel in `/boot` if that is a
     different file, the installer offers
     `dpkg-statoverride --update --add root root 0644 /boot/vmlinuz-…`.
   - It records the previous mode. It leaves an existing override alone.
   - A later kernel package brings a new 0600 file. `rehome doctor` then
     reports it again, and `install.sh --update` offers the fix again.

7. **Files.** In a single root command, the installer runs
   `layout.py install --installer-copy --manifest`, with the recorded packages,
   group and override as `--manifest-data`.

8. **Check.** It runs `rehome doctor --quick` (as the invoking user) and shows
   the result.

If the installer stops after changing something but before the manifest
exists, it lists those changes so they can be undone.

### Manifest

`PREFIX/share/rehome/install-manifest.json` (abridged):

```json
{
  "app": "rehome", "schema": 1, "version": "0.1.0",
  "prefix": "/usr/local", "libexecdir": "/usr/local/libexec/rehome", "python": "/usr/bin/python3",
  "source": "release v0.1.0 (https://github.com/…/rehome-0.1.0.tar.gz)",
  "installed_at": "2026-10-20T21:00:00+00:00", "updated_at": "2026-10-20T21:00:00+00:00",
  "distro": {"id": "ubuntu", "name": "Ubuntu 24.04.5 LTS", "family": "debian"},
  "files": ["/usr/local/lib/rehome/rehome/__init__.py", "…", "/usr/local/share/rehome/install-manifest.json"],
  "directories": ["/usr/local/share/metainfo", "…"],
  "packages": {"manager": "apt", "installed": ["python3", "qemu-system-x86", "…"]},
  "groups": [{"user": "alice", "group": "kvm"}],
  "statoverrides": [{"path": "/boot/vmlinuz-6.8.0-45-generic", "previous_mode": "600",
                     "owner": "root", "group": "root", "mode": "0644"}]
}
```

`--update` merges into the existing manifest: package, group and override
records are kept, and so is `installed_at`.

### Uninstall order and how packages are removed

1. **Files.** `layout.py uninstall --manifest`, using the installed copy. This
   step needs Python, which may itself be one of the packages removed later, so
   it runs first. The manifest is read into memory beforehand.
2. **Overrides.** `dpkg-statoverride --remove`, then `chmod` back to the
   recorded mode.
3. **Group.** `gpasswd -d USER kvm`.
4. **Packages.** Unless `--keep-deps` is given, the installer lists the
   packages and asks before removing them. Packages that other installed
   software still needs are always kept and reported:

   | Family | How it removes them |
   |---|---|
   | apt | `apt-mark auto PKGS`, then `apt-get autoremove`. It removes only packages nothing else depends on. If removal fails or is declined, the manual marks are restored. |
   | pacman | `pacman -Rsu`. `-u` skips any target another package needs. |
   | dnf / zypper | `rpm -e --test` finds the subset no other installed package requires (iterating through capabilities and providers), then `dnf remove` or `zypper remove --clean-deps` removes it. |

   Without `--yes`, the package manager shows its own transaction and asks
   again.

   Packages' configuration files are kept, with `--purge` too. Purging a
   dependency can delete system files it adopted: in testing, purging
   `locales` (pulled in indirectly) deleted the pre-existing
   `/etc/default/locale`. `--purge` only adds the deletion of Rehome's own
   user data.

Every step checks the current state first, so an interrupted uninstall can be
run again.

**apt can keep a few dependencies.** After `apt-get autoremove`, a few of the
dependencies the install pulled in can stay installed. apt keeps an
automatically installed package that an installed package Recommends or
Suggests, lists as an alternative (`a | b`), or provides a virtual package for.
On a minimal Ubuntu 24.04 container, 12 packages stayed for this reason:

| Kept package(s) | Held by |
|---|---|
| ca-certificates and openssl | `apt` Recommends ca-certificates |
| adduser | `apt` depends on `base-passwd \| adduser` |
| gawk and its libraries | `base-files` needs the virtual `awk` |
| psmisc | Recommended or suggested by `procps` |
| uuid-runtime | Recommended or suggested by `libuuid1` |

This is apt's policy, not a leftover of Rehome; desktop systems normally have
these packages already. The tests check that nothing pre-existing was removed,
that no manual mark changed, and that every remaining package is held this way
(`tests/packaging/apt_kept.py`).

**Maintainer scripts leave traces too.** The dependencies' maintainer scripts
also leave things behind that dpkg does not remove without a configuration
purge:

- system users' state directories (for example `/var/lib/swtpm-localca`);
- enabled systemd unit links;
- empty directories such as `/usr/local/share/ca-certificates`
  (ca-certificates) and `/usr/local/share/fonts` (fontconfig-config);
- `/etc/default/locale`, which the `locales` package moves to
  `/etc/locale.conf` and replaces with a symlink. The content is unchanged.

The container tests check that none of these mention Rehome, that no file was
added under `/usr/local`, that no pre-existing path was deleted, and that the
locale setting's content is unchanged.

### Environment variables (mostly for tests)

| Variable | Effect |
|---|---|
| `REHOME_SOURCE` | Same as `--source`. |
| `REHOME_RELEASES_API` | Release metadata URL. Default: `https://api.github.com/repos/oreo1298/Disk-to-VM-Manager/releases/latest`. |
| `REHOME_MAIN_TARBALL` | Fallback tarball when no release exists. |
| `REHOME_OS_RELEASE` | os-release file to read instead of `/etc/os-release`, for `--detect` tests. |
| `REHOME_INSTALLER_NO_MAIN=1` | Source the script without running it, to test its functions. |
| `NO_COLOR` | Disables colours. |

## Building the packages

```
make deb        # packaging/deb/build.sh  -> dist/rehome_<ver>_all.deb
make rpm        # packaging/rpm/build.sh  -> dist/rehome-<ver>-1.noarch.rpm   (--suse, --srpm)
make dist       # packaging/dist.sh       -> dist/rehome-<ver>.tar.gz + .sha256
make srcinfo    # cd packaging/arch && makepkg --printsrcinfo > .SRCINFO
```

### .deb

`packaging/deb/build.sh` builds the package without debhelper:

- It stages `layout.py install --prefix /usr --libexecdir /usr/libexec/rehome
  --destdir STAGE`.
- `Depends` and `Recommends` come from the `debian` entries of `packages.toml`,
  with `python3 (>= 3.11)`.
- It adds `/usr/share/doc/rehome/copyright` and `DEBIAN/md5sums`.
- It packs with `dpkg-deb --root-owner-group`, so no fakeroot is needed.
- Pre-release versions are mapped so they sort correctly
  (`0.2.0rc1` → `0.2.0~rc1`).

### .rpm

`packaging/rpm/rehome.spec` is one spec for Fedora (and RHEL-likes) and
openSUSE: `%if 0%{?suse_version}` selects the openSUSE package names.

`packaging/rpm/build.sh` builds it:

- It makes the `Source0` tarball with `dist.sh`, using GitHub's tag-archive
  top directory (`Disk-to-VM-Manager-<ver>/`), so the same spec also builds
  from a release tag on COPR or OBS.
- It runs `rpmbuild -bb` (`-ba` with `--srpm`).
- On hosts that are not RPM-based (such as Ubuntu with the `rpm` package) it
  adds `--nodeps`, because there is no RPM database to check `BuildRequires`
  against.

The spec turns off debuginfo and Python bytecompilation; the launcher keeps
bytecode in the user's cache instead.

### Arch / AUR

`packaging/arch/PKGBUILD`:

- `arch=any`, `license=('GPL-3.0-or-later')`.
- `depends` and `optdepends` follow the `arch` entries of `packages.toml`.
- `package()` runs `layout.py install --prefix /usr --libexecdir
  /usr/lib/rehome`.
- The source is the GitHub tag archive `v$pkgver.tar.gz`.

`sha256sums` is `SKIP` until the first tag exists.

### Release checklist

1. Bump `__version__` in `rehome/__init__.py`, `pkgver` in `PKGBUILD`,
   `Version` and `%changelog` in `rehome.spec`, and add a `<release>` to the
   metainfo. `tests/unit/test_layout.py` fails if the PKGBUILD or the spec
   disagree with `__version__`.
2. Tag `vX.Y.Z` and push the tag.
3. Run `make dist`, then upload `dist/rehome-X.Y.Z.tar.gz` and its `.sha256` to
   the GitHub release. `install.sh` downloads that asset and verifies it.
4. In `packaging/arch`, run `updpkgsums && makepkg --printsrcinfo > .SRCINFO`,
   then push both files to the AUR.
5. Attach the `.deb` and `.rpm` files from `make deb rpm` to the release.

The Fedora, openSUSE and Arch package names in `packages.toml` could not be
checked against live repositories during development (see "Testing" below).
The installer's one-at-a-time fallback reports any wrong name precisely; fix
the name in `packages.toml`, the spec and the PKGBUILD together.

## Opt-in update check

`rehome/update.py` and `rehome update-check`:

- **Off by default.**
  - `rehome update-check --enable` sets `update_check = true` in
    `~/.config/rehome/settings.toml`; `--disable` turns it off.
  - Other keys in the file are preserved.
  - Anything other than the boolean `true` counts as off.
- **The request.** When enabled, `update.maybe_check()` (for the GUI) makes at
  most one request a day. It is a single anonymous GET to GitHub's
  `releases/latest` API.
  - No cookies, IDs or system details are sent. The only headers are
    `User-Agent: rehome/<version>` and the `Accept` header.
  - The time of the last check is cached in `~/.cache/rehome/update-check.json`.
  - Network and parse errors are returned in the result, never raised.
- **Manual check.** Running `rehome update-check` checks once, because you
  asked for it.
- **How to update.** The command then explains how, depending on how Rehome
  was installed:
  - an install manifest exists: `install.sh --update`, through the one-liner or
    the installed copy;
  - a git checkout: `git pull`;
  - otherwise: the distribution's way (AUR helper, `apt install
    ./rehome_*.deb`, `dnf upgrade`, `zypper update`).

Nothing is ever downloaded or installed automatically.

## Testing

- **Unit tests** (`python3 -m pytest tests/unit/test_layout.py
  tests/unit/test_update.py`):
  - the layout and its rendered templates;
  - a manifest install, update and uninstall round trip that leaves an empty
    `DESTDIR`, and refusal of paths outside Rehome's locations;
  - the generated launcher really running;
  - `install.sh --detect` for 16 os-release samples;
  - the PKGBUILD, `.SRCINFO` and spec matching `packages.toml` and the version;
  - version comparison, the opt-in setting, the request headers, network
    errors and the install-method hints. These run without network access.

- **Container tests**: `tests/packaging/run.sh [TEST…]`. Logs go to
  `tests/.cache/packaging/logs/`. The script skips when Docker is not
  available. Every test uses one snapshot of the source tree (taken with
  `dist.sh`), so edits made while it runs cannot mix versions.

  | Test | What it does |
  |---|---|
  | `lint` | shellcheck on `install.sh`, the build scripts, the tests and the PKGBUILD |
  | `detect` | 17 os-release samples |
  | `distro-{arch,debian,fedora,suse}` | See below |
  | `arch-pkg` | See below |
  | `rpm-fedora`, `rpm-suse` | See below |
  | `deb` | See below |
  | `ubuntu-full` | See below |
  | `ubuntu-pipe` | See below |

  - **`distro-{arch,debian,fedora,suse}`**
    - detection;
    - `--dry-run` without python3 (the exact bootstrap command);
    - a real run without python3 (clear error, nothing changed);
    - the exact `--dry-run` package commands, required and with
      `--with-optional`;
    - declining the first root command through a pseudo-terminal (nothing
      changed; skipped where `script` is not installed);
    - a real install whose package installs fail (one-at-a-time fallback,
      exact list of failed names, Rehome installed anyway, clean uninstall);
    - a file-only `--skip-deps` round trip: install, `rehome --version`,
      `--update`, "already up to date", `--uninstall`, `--purge`. Afterwards
      the filesystem snapshot is identical to the one taken before.
  - **`arch-pkg`**
    - `bash -n`;
    - `makepkg --printsrcinfo` matches `.SRCINFO`;
    - `makepkg -d` offline, from a local tag-style tarball;
    - the package's files match `layout.py list`;
    - `pacman -U`, `pacman -Qkk`, `rehome --version`, `pacman -R`, with
      nothing left behind;
    - install.sh's `pacman -Rsu` removal with dummy packages: the package
      another one needs is kept.
  - **`rpm-fedora`, `rpm-suse`**
    - `rpm -i --nodeps`;
    - files match the layout;
    - `rpm -V`, `rehome --version`, `rpm -e`, with nothing left behind;
    - install.sh's removal logic with dummy RPMs: `rpm_removable` keeps
      directly and indirectly required packages; `dnf remove` and
      `zypper remove --clean-deps` remove the rest.
  - **`deb`**
    - `apt install ./rehome_*.deb` with real dependency resolution from the
      Ubuntu archive;
    - `rehome --version` and `rehome doctor --quick` as a user (helper,
      policy and QEMU detected);
    - files match the layout; `dpkg --verify`;
    - `apt purge rehome` plus `apt autoremove`;
    - the apt-policy leftover check, with a negative control.
  - **`ubuntu-full`**: a sudo user installs from a checkout with real
    dependencies.
    - Every sudo command was printed first.
    - The recorded packages are exactly the newly installed ones.
    - The kvm group is added, and the statoverride applied to a stand-in
      kernel file.
    - The interactive optional selection works through a pseudo-terminal.
    - `--update`: new and removed data files, VMs untouched, "already up to
      date", downgrade refused.
    - `--uninstall` from the installed copy: files, directories, packages,
      group and override all reverted; data kept.
    - `--purge` deletes the data.
  - **`ubuntu-pipe`**: the one-liner `curl … | bash` as root against a mock
    GitHub release server.
    - `--with-optional`.
    - SHA-256 verified.
    - `--update` through the API.
    - `--purge`: the recorded packages are removed (their configuration
      files are kept) and the data is deleted.
    - A tampered checksum aborts and nothing is installed.
    - With no release, it falls back to the main branch.

### Test scaffolding

The scaffolding is disclosed here because it affects what the results prove:

- **Blocked mirrors.**
  - The Arch, Debian, Fedora and openSUSE package mirrors were unreachable
    from the development sandbox, and their images have no python3.
  - Those tests mount a relocatable Python 3.12 (plus fakeroot for makepkg),
    built from Ubuntu 24.04 packages by `tests/packaging/make-bundle.sh`.
  - Real dependency installation on those four distributions was therefore
    not tested. What was tested there: the exact commands, the failure
    handling, and the file and package round trips.
- **Arch.** The makepkg test turns off the `debug` option for its build user,
  because debugedit (part of base-devel) is not in the image.
- **RPM.** The RPM tests disable the unreachable repositories so that
  `dnf remove` and `zypper remove` can run. They also create the polkit and
  hicolor directories first; on a real system those belong to packages Rehome
  requires.
- **Ubuntu.** The Ubuntu tests:
  - create a `kvm` group if the image has none;
  - use a stand-in `/boot/vmlinuz-$(uname -r)` with mode 0600, since
    containers have no kernel in `/boot`;
  - keep downloaded `.deb` files in a Docker volume between runs.

## Flatpak and Snap (decision D13, extended)

Rehome ships as native packages only. Flatpak and Snap were evaluated against
what Rehome must do on the host.

**1. Physical disks need host-side privilege.**
- A disk is opened read-only and `O_EXCL` by `rehome-helper` through pkexec.
  The helper checks first that the disk is not the host's root, `/boot` or
  swap, and passes the file descriptor back.
- The polkit policy must be installed in `/usr/share/polkit-1/actions` on the
  host, with the helper's absolute host path.
  - A Flatpak cannot install files there.
  - Inside the sandbox pkexec does not work: Flatpak sets `no_new_privs`, so
    setuid binaries cannot gain privileges.
  - `flatpak-spawn --host pkexec` would still need a helper installed on the
    host, which means a native package anyway.
- `--device=all` exposes `/dev` but not root's permissions on block devices.
  Granting it plus host filesystem access would remove most of what the
  sandbox is for.
- Snap's block-device and polkit interfaces are privileged. They are not
  auto-connected for store snaps and need a manual connection or store
  approval.

**2. libguestfs needs the host kernel and `/dev/kvm`.**
- supermin builds the inspection appliance from the host's
  `/boot/vmlinuz-*` and `/lib/modules`. A runtime has neither, and the sandbox
  does not show the host's `/boot`.
- A Flatpak would have to bundle a prebuilt appliance (hundreds of MB),
  including a kernel, and keep it patched separately from the host.
- `/dev/kvm` is available to Flatpaks (`--device=kvm`), so this part is
  solvable. The size and the security-update burden are not.

**3. Sidecar programs do not fit a nested sandbox.**
- **qemu-bridge-helper** is setuid root, and setuid cannot work under
  `no_new_privs`, so bridged networking is impossible.
- **virtiofsd** sandboxes itself with namespaces; nested inside bubblewrap that
  needs `--sandbox=none`.
- **passt** also uses namespaces and seccomp.
- **swtpm** works, but would be bundled.
- QEMU itself would have to be bundled too, as GNOME Boxes does, and could not
  use the distribution's tuned firmware descriptors in
  `/usr/share/qemu/firmware`.

**4. USB pass-through** uses the same helper and fd-passing as disks. The newer
xdg-desktop-portal USB portal could replace it inside a sandbox one day, but
it is not yet available on the distributions Rehome targets.

**5. Snap classic confinement** would work technically. It is essentially a
.deb with no sandbox, it needs manual store review, and it still cannot
install a polkit policy without a privileged interface. It offers no advantage
over the native packages.

**AppImage** has the same polkit and host-helper problem, and adds bundled
GTK and libadwaita that do not follow the system's versions.

**Possible later:** a Flatpak edition limited to "create and manage VMs, import
disk images" (no physical disks, bridged networking or host-kernel libguestfs).
It would bundle QEMU, swtpm and a fixed libguestfs appliance and use
`--device=kvm`. An optional small native package could add the helper and
policy for disk import, which the Flatpak would call through
`flatpak-spawn --host` (and so give up the sandbox for that step). Revisit
when the USB portal and a portal for block devices are widely available.
