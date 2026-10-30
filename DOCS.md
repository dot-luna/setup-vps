# VPS setup

## Run

Save `setup-deb.sh` and run `bash setup-deb.sh` from an interactive SSH terminal.
The script elevates through sudo when necessary. `--help` prints usage.

Supported hosts: Debian 12/13 and Ubuntu 22.04/24.04/26.04, amd64 or arm64,
with systemd running. Internet access to distro repositories, Docker, mise,
GitHub, and mise's tool registries is required. Ubuntu tools require the standard
Universe repository to be enabled.

## What happens

1. Validate the OS, architecture, and existing Docker package conflicts.
2. Update apt indexes and upgrade installed packages, keeping existing system
   configuration. Upgrades may restart services. Setup never reboots the VPS.
3. When run directly as root, ask whether to create a user. A new user gets a
   password and membership in `sudo`. Declining configures root. Through sudo,
   setup configures the original user.
4. Install packages, enable Docker at boot, and configure the selected user's
   tools, plugins, and default Zsh shell.
5. Ask for each missing global Git identity field (`user.name`, `user.email`).
6. Offer an Ed25519 key at `~/.ssh/id_ed25519`. ssh-keygen prompts for an optional
   passphrase. Setup prints the public key and private key path. Existing private
   or public key paths are kept. This key is for outgoing connections, such as
   GitHub; incoming VPS login needs your computer's public key in
   `~/.ssh/authorized_keys`.
7. Run `apt-get autoremove -y` to remove unused dependencies, then verify the
   tools, Zsh integrations, and Docker daemon. Print login and reboot
   guidance. Reconnect to activate the default shell and changed groups.

Yes/no prompts default to **no**. Closed input or a command failure stops setup
with a nonzero exit. Completed package/account changes remain; fix the cause and
rerun. To configure an existing non-root user on a rerun, run through that user's
sudo session.

## Installed tools

| Source | Tools |
| --- | --- |
| Distro apt | Git, curl, wget, CA certificates, GPG, sudo, adduser/passwd, build-essential, zip/unzip, xz, jq, ripgrep, fd-find, bat, zoxide, btop, tmux, rsync, kitty-terminfo, Zsh, OpenSSH client, util-linux |
| Distro apt | fastfetch when available, otherwise neofetch |
| Official Docker apt repository | Docker Engine/CLI, containerd, Compose plugin, Buildx plugin |
| mise | Latest fzf, eza, Starship; no language runtimes installed |
| Upstream Git repositories | fzf-tab, zsh-autosuggestions, zsh-syntax-highlighting, zsh-completions |

Docker uses a repository-specific signing key. Conflicting distribution Docker,
containerd, or runc packages cause setup to stop before upgrading; remove those
packages deliberately before rerunning. Adding the user to `docker` is optional
and grants root-level access. Docker-published ports can bypass UFW rules; see
[Docker's firewall guidance](https://docs.docker.com/engine/network/packet-filtering-firewalls/).

Mise installs into `~/.local/bin`; its default download verification remains
enabled. Setup uses its [official Linux installer](https://mise.jdx.dev/installing-mise.html)
and installs current tool releases. Plugin checkouts use upstream default
branches. A rerun can therefore update the installed tools and plugins.

## Shell and files

Zsh provides Starship, persistent history, Up/Down prefix search, Ctrl+R history,
Ctrl+T file search, and Alt+C directory search. `cd` uses zoxide; `cdi` selects
interactively. `ll`, `la`, and `lt` use eza. `cat` and `ls` keep their normal
behavior. `bat` and `fd` links expose Debian's `batcat` and `fdfind` names.
Run `fastfetch` or `neofetch` manually to show system information.

These selected-user exports are replaced without backups on every run:

- `~/.zshrc`
- `~/.config/setup-vps/{zshrc,starship.toml,mise-completion.zsh}`
- `~/.local/share/setup-vps/plugins/`
- `~/.local/bin/{bat,fd}`

Setup also writes `/etc/apt/keyrings/docker.asc` and
`/etc/apt/sources.list.d/docker.sources`, updates mise's global tool entries, and
fills missing global Git identity fields. It leaves unrelated paths untouched.
It does not configure a firewall, change SSH server authentication, or install a
Kitty GUI; `kitty-terminfo` supports Kitty clients over SSH.

## Checks

Run with Python 3, Bash, Zsh, ShellCheck, OpenSSH client, and util-linux:

```sh
bash -n setup-deb.sh
shellcheck setup-deb.sh
python3 -m unittest discover -s tests -v
```

Tests use temporary homes and stub privileged host operations. CI also checks
real apt package availability across every supported distro release. Full
service startup and interactive login require a systemd VPS.
