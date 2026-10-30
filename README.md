# setup-vps

One interactive script to update a Debian/Ubuntu VPS, install server tools and
Docker, and configure Zsh like [Luna Next](https://github.com/dot-luna/luna-next).

Use a systemd VPS with Debian 12/13 or Ubuntu 22.04/24.04/26.04, on amd64/arm64.
Run from a terminal as root or a user with sudo access. Download with curl:

```sh
curl -fsSLO https://raw.githubusercontent.com/dot-luna/setup-vps/main/setup-deb.sh
bash setup-deb.sh
```

See [DOCS.md](DOCS.md) for prompts, replaced files, installed tools, and checks.
