**A preview: a console system, no desktop yet.**

GraveyardOS 0.1 is the Linux From Scratch 13.1 base system built in CI, with
its own kernel (7.1.8), initramfs and package manager (Yone), as a live ISO.
It boots to a root shell, and it can install itself on a disk. The desktops
(KDE Plasma, Hyprland) and the NVIDIA driver come with the next images.

**What was tested** (QEMU, in CI, on this very ISO): BIOS and UEFI, as a disc
and as a USB stick; from a Ventoy stick (BIOS and UEFI); network by DHCP; a
full installation on an empty disk, then the installed system started from
that disk alone (UEFI and BIOS). Secure Boot must be off.

**Write it to a USB stick** (this erases the stick; check the device name
with `lsblk`), or copy it to a Ventoy stick:

```
sha256sum -c GraveyardOS-console-0.1-x86_64.iso.sha256
sudo dd if=GraveyardOS-console-0.1-x86_64.iso of=/dev/sdX bs=4M status=progress oflag=sync
```

At the boot menu, choose GraveyardOS. You are logged in as root on the first
console. To install on this computer, run `graveyard-installer --listen
0.0.0.0` and open the address it prints from another computer's browser
(the console has none); the installation erases the disk you choose.
