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
2. When run directly as root, ask whether to create a user and collect the
   username. Declining selects root. Through sudo, select the original user.
3. Collect Git identity before package updates. Use the selected Linux username
   for a missing `user.name` and ask for a missing `user.email`. Existing Git
   identity is kept. If Git or runuser is unavailable, collect an email candidate
   now and recheck existing config after installation.
4. Update apt indexes, upgrade installed packages, and install tools. Keep
   existing system configuration. Upgrades may restart services; setup never
   reboots the VPS.
5. Create the selected new account, prompt for its password, and add it to
   `sudo`. Apply missing Git identity fields, enable Docker at boot, and
   configure the selected user's tools, plugins, and default Zsh shell.
6. Run `apt-get autoremove -y` and verify the tools, Docker, selected user's Zsh
   startup files, login shell, and plugin loading.
7. Print the configured home, `.zshrc` path, and login command. The existing
   terminal keeps its current shell; use `sudo -iu USER` to open the configured
   account's login shell. Reconnect to activate changed groups.

Yes/no prompts default to **no**. Closed input or a command failure stops setup
with a nonzero exit. Completed package/account changes remain; fix the cause and
rerun. To configure an existing non-root user on a rerun, run through that user's
sudo session.

Terminal output uses bright setup headings and dim gray tool logs, with warnings
in yellow and errors in red. Redirected output, `TERM=dumb`, and `NO_COLOR=1`
disable these colors. Failed commands report the phase and selected user/home.
SSH keys are managed separately; setup does not generate or replace them.

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

Starship uses Luna-style bracketed modules with a built-in Tokyo Night palette,
two-component directory truncation, and a cyan Git repository root. Its colors
are defined in `~/.config/setup-vps/starship.toml`; no Kitty or Luna theme files
are needed. The prompt colors work over SSH in a terminal with true-color
support. The terminal's background and font are still configured locally.

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
real apt package availability across every supported distro release and renders
the Tokyo Night prompt with native Starship on Debian 13. The native rendering
test skips when Starship is unavailable. Full
service startup and interactive login require a systemd VPS.
