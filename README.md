<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="branding/graveyard-logo-mono-white.svg">
    <img src="branding/graveyard-logo-mono-black.svg" alt="GraveyardOS" width="120">
  </picture>
</p>

GraveyardOS is an independently built x86-64 Linux distribution: every
package compiled from source, starting from [Linux From Scratch](https://www.linuxfromscratch.org/)
13.1 with systemd, managed by its own package manager, **Yone**, written in
Rust. Rolling release, prebuilt binaries, two desktops (KDE Plasma on Wayland,
Hyprland), black and white.

Learn more at **[graveyardos.pages.dev](https://graveyardos.pages.dev)**.

> In development. What exists is built and tested in CI; what does not is in
> [ROADMAP.md](ROADMAP.md). Today: the console live ISO (with its installer);
> the desktop images are being built.

## Guide

### 1. Get it

Download the ISO from [the site](https://graveyardos.pages.dev) or the
[releases](https://github.com/tfem1/graveyardos/releases), check it, write it
to a USB stick (this erases the stick: check the device with `lsblk`):

```sh
sha256sum -c GraveyardOS-console-0.1-x86_64.iso.sha256
sudo dd if=GraveyardOS-console-0.1-x86_64.iso of=/dev/sdX bs=4M status=progress oflag=sync
```

or copy the file to a [Ventoy](https://www.ventoy.net) stick. It starts on BIOS
and UEFI computers; Secure Boot must be off.

### 2. Start it

At the boot menu choose **GraveyardOS live**. Other entries: **NVIDIA** (the
open kernel modules and NVIDIA's driver, on the desktop images), **safe
graphics** (for a screen that stays black), firmware settings, restart, power
off. The live system runs in memory: nothing is written to the computer.

### 3. Install it

On a desktop image the installer opens by itself. On the console image run
`graveyard-installer --listen 0.0.0.0` and open the address it prints from
another computer. Choose the disk (automatic, or your own partitions), the
language, keyboard, time zone, your user, optional disk encryption, the
desktop and NVIDIA; the summary says exactly what will be erased before
anything is.

### 4. Use it

Yone installs, updates and checks the software:

```sh
sudo yone update            # fresh signed indexes, then every package
sudo yone install firefox   # with what it needs, downloaded in parallel, installed atomically
sudo yone remove firefox
yone search browser         # names and descriptions
yone list                   # every package the repositories offer, [installed] marked
yone list --installed       # what is installed (--explicit, --deps)
yone info firefox
sudo yone autoremove        # dependencies nothing needs anymore
sudo yone verify            # every installed file against its checksum
sudo yone recover           # finish or undo a transaction cut short
```

`yone help` lists every command. Packages are signed (ed25519); a transaction
either completes or leaves the system as it was.

### 5. Build it

Everything is built by the workflows in [`.github/workflows`](.github/workflows),
from the recipes in this repository; [BUILDING.md](BUILDING.md) explains each
step and how to run it yourself.

## What is where

| | |
|---|---|
| [`yone/`](yone) | the package manager (Rust, no third-party crates) |
| [`lfs/`](lfs) | the Linux From Scratch book as build scripts, and the build runner |
| [`packages/`](packages) | the base system as Yone packages, generated from the book |
| [`arch/`](arch) | Arch Linux's recipes (PKGBUILD) converted to Yone recipes, for the desktops |
| [`kernel/`](kernel) | the kernel configuration |
| [`system/`](system) | the system's configuration and the initramfs |
| [`installer/`](installer) | the installer (a local web page and its engine) |
| [`iso/`](iso) | the live images (console, KDE, Hyprland), the NVIDIA layer, boot tests |
| [`branding/`](branding) | logo, wallpapers, boot menu, login screen, Fastfetch |
| [`site/`](site) | the site, graveyardos.pages.dev |
| [`tools/`](tools) | small system tools (GPU detection...) |

## Project

[ROADMAP.md](ROADMAP.md) (what is done, what comes) ·
[DECISIONS.md](DECISIONS.md) (the choices and why) ·
[ARCHITECTURE.md](ARCHITECTURE.md) ·
[SECURITY.md](SECURITY.md) (trust model, reporting a vulnerability)

One branch, `main`; releases are tags. Free software under the
[GNU GPL v3 or later](LICENSE); the name and logo are reserved
([TRADEMARKS.md](TRADEMARKS.md), [NOTICE.md](NOTICE.md)). © 2026 tfem1.
