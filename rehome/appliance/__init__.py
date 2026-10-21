"""Rehome appliances: a VM packaged with a minimal Linux that boots straight into it.

The builder (``base``, ``build``) runs on the user's computer, without root,
and writes a disk image. The rest (``kiosk``, ``console``, ``firstboot``,
``hardware``) runs inside the appliance. See docs/APPLIANCE.md.
"""
