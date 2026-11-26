#!/bin/sh
# One-time setup on the VPS for the Obsidian bridge. Run with sudo, after the new code is in
# /srv/nexus/app (DEPLOYED_COMMIT updated) and this directory is copied to /tmp/obsidian-deploy.
#
#   /srv/obsidian/vault      the vault (canonical copy); Syncthing shares it with the owner's machines
#   /srv/obsidian/state      the bridge's state file (node ids, last synced text); not shared
#   /srv/obsidian/syncthing  Syncthing's own config and keys
#
# Both services run as "nexus"; systemd sandboxing keeps Syncthing (the network-facing one) away
# from /srv/nexus (database, backups, code): it can only read and write the vault and its config.
set -eu

# 0. a backup of the primary before the bridge writes to it for the first time
/srv/nexus-bin/nexus-backup

# 1. folders
install -d -o nexus -g nexus -m 0750 /srv/obsidian /srv/obsidian/vault /srv/obsidian/state /srv/obsidian/syncthing
printf '.trash\n.nexus-tmp-*\n(?d).DS_Store\n' > /srv/obsidian/vault/.stignore
chown nexus:nexus /srv/obsidian/vault/.stignore

# 2. Syncthing (Ubuntu universe package); GUI on 127.0.0.1:8384 only, reach it with ssh -L
apt-get install -y syncthing
install -m 0644 /tmp/obsidian-deploy/syncthing-obsidian.service /etc/systemd/system/
# 3. the bridge
install -m 0644 /tmp/obsidian-deploy/nexus-obsidian.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now syncthing-obsidian.service nexus-obsidian.service
sleep 8
systemctl --no-pager --lines=0 status syncthing-obsidian nexus-obsidian | grep -E "Loaded|Active"
journalctl -u nexus-obsidian -n 3 --no-pager
