<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/img/beacon-lockup-dark.png">
    <img src="docs/img/beacon-lockup-light.png" alt="Beacon" width="480">
  </picture>
</p>

# Beacon

[![Platform](https://img.shields.io/badge/Platform-Windows_10%2F11-blue.svg)](#install)
[![Game](https://img.shields.io/badge/Game-Subnautica_2-darkgreen.svg)](https://store.steampowered.com/app/1962700/)
[![Server Source](https://img.shields.io/badge/Server_Source-BeaconServer-brightgreen.svg)](https://github.com/HumanGenome/BeaconServer)

Beacon gives **Subnautica 2** IP/port multiplayer: players join through the Beacon desktop app, hosts run a Beacon server package next to Subnautica 2, and worlds stay on the server with snapshots, rollback, admin tools, server query, and mod support. No friend-code lobby, no peer-to-peer handoff, no four-player ceiling.

Every player must install Beacon to join a Beacon server. Stock Subnautica 2 cannot connect to Beacon servers directly.

<p align="center">
  <img src="https://raw.githubusercontent.com/HumanGenome/Beacon/main/docs/img/launcher.png" alt="Beacon launcher showing a Subnautica 2 server" width="860">
</p>

## Features

### 🗺 Live map
See who's online on a real, interactive map of the world. The server publishes live player positions and the launcher opens a browser map that plots each player and updates in real time, on the full Subnautica 2 world with its Points of Interest. Open it per-server from the launcher's Map button, or straight from the host's `/map/` page.

<p align="center">
  <img src="https://raw.githubusercontent.com/HumanGenome/Beacon/main/docs/img/live-map.gif" alt="Beacon live web map tracking a player in Subnautica 2" width="860">
</p>

### 💬 In-game chat
Press Enter to open chat without leaving the game. Beacon overlays a timestamped chat panel in Subnautica 2 with player messages, server notices, and admin announcements. Admins post announcements and a message of the day from the server console or RCON, and players can run commands like `/players` and `/help` straight from the chat box.

<p align="center">
  <img src="https://raw.githubusercontent.com/HumanGenome/Beacon/main/docs/img/chat.jpg" alt="Beacon in-game chat overlay in Subnautica 2" width="860">
</p>

### 👥 Bigger crews
Stock Subnautica 2 caps co-op at four players. Beacon runs your whole crew on one server, past that limit.

### 🧭 Join by IP and port
Add a server address once, pick your character, and connect straight into the hosted world.

### 🧑‍🚀 Multiple characters
Players can keep separate characters per server. Beacon remembers the character you used for each saved server.

### 🔁 Snapshots and rollback
The server snapshots the world on every auto-save, and admins can trigger and list snapshots over RCON:

```
> save snapshot
snapshot ok: snap-20260610T141503Z-7c9e2ab04d113e (52428800 bytes, sha=ab12cd34ef56ab12)
> save list
snap-20260610T141503Z-7c9e2ab04d113e  52428800B  age=42s  sha=ab12cd34ef56ab12
```

Restore by ID from the launcher's world tools or the admin HTTP API (`POST /api/v1/snapshots/<id>/restore`). The server takes a pre-restore snapshot, swaps the world in, and relaunches Subnautica 2.

### 🛠 Admin console
BeaconServer exposes Source RCON (default TCP 27018) with built-ins like `status`, `players`, `save snapshot`, and `motd`, plus slash commands registered by server mods:

```
> announce Server restarts in 10 minutes
chat ok (admin): Server restarts in 10 minutes
```

`announce` posts an admin notice to every connected player's in-game chat overlay.

### 📡 Server query
BeaconServer answers Source A2S query (default UDP 27017), so server browsers, monitoring tools, and bots can read live status and player counts:

```
Server name : Reef Runners
Map         : Awake
Game        : Subnautica 2
Players     : 3 / 4
Version     : beacon-0.3.102/sn2-115506
```

### 🧩 Mods
Beacon uses UE4SS for Lua and C++ mod loading on the client and host runtime. Servers declare mods in `appsettings.json` (`Beacon:Mods`); the launcher fetches `GET /api/v1/manifest` on join and installs required mods automatically:

```json
"Mods": {
  "Required": [{
    "Id": "beacon.chat",
    "Name": "Beacon Chat Overlay",
    "Version": "1.0.0",
    "Url": "https://github.com/HumanGenome/Beacon/releases/download/v0.4.0/BeaconChat-1.0.0.zip",
    "InstallRoot": "ue4ss/Mods/BeaconChat"
  }]
}
```

## Install

### Managed hosting
The easiest option is a [SurvivalServers.com Subnautica 2 server](https://www.survivalservers.com/services/game_servers/subnautica_2/?utm_source=github&utm_medium=readme_install&utm_campaign=beacon) (official hosting): BeaconServer comes preinstalled and the ports are already configured.

### Players
1. Download `BeaconSetup-<version>.exe` from the [latest release](https://github.com/HumanGenome/Beacon/releases/latest).
2. Run the installer. Windows SmartScreen may warn because the installer is not code-signed yet; choose **More info** then **Run anyway**.
3. Open Beacon, add the server address, select or create a character, and click **Connect**.

Beacon also checks for launcher updates automatically on launch and while it is running.

### Self-hosted servers
1. Download `Beacon-Server-Windows-x64-v<version>.zip` from the [latest BeaconServer release](https://github.com/HumanGenome/BeaconServer/releases/latest).
2. Extract it on the Windows host.
3. Edit `BeaconServer\appsettings.json`.
4. Forward/open the gameplay UDP port, query UDP port, RCON TCP port, and admin HTTP TCP port.
5. Run `BeaconServer\BeaconServer.exe`.

Full server setup, settings, source query examples, RCON commands, and build instructions live in [HumanGenome/BeaconServer](https://github.com/HumanGenome/BeaconServer).

## Releases

This repo publishes the launcher only:

- `BeaconSetup-<version>.exe` — installer for players
- `BeaconSetup-latest.exe` — stable download URL that always points at the most recent installer
- `Beacon-Launcher-Windows-x64-v<version>.zip` — portable launcher build
- `checksums-launcher.txt` — hashes for the launcher assets

The dedicated server package (`Beacon-Server-Windows-x64-v<version>.zip`) lives on the [BeaconServer release page](https://github.com/HumanGenome/BeaconServer/releases/latest).

Source archives are generated by GitHub automatically for tags.

## Source

Beacon is split into two repos:

- **Beacon** (this repo) — the client side: the desktop launcher app players install, plus the public downloads and documentation.
- **[BeaconServer](https://github.com/HumanGenome/BeaconServer)** — the dedicated server hosts run next to Subnautica 2. Open source.

Players only need this repo's releases; hosts run the server from BeaconServer's.

## Community Note

Beacon is a community project and is not affiliated with or endorsed by the developers of Subnautica 2.

## License

See [LICENSE](LICENSE).

## Credits

- [UE4SS](https://github.com/UE4SS-RE/RE-UE4SS) — Unreal Engine scripting and modding framework
- [Avalonia](https://avaloniaui.net/) — .NET UI framework used by Beacon
- [joric/subnautica](https://github.com/joric/subnautica) (Unlicense) — the interactive Subnautica 2 map that powers Beacon's live web map
