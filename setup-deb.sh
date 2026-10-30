#!/usr/bin/env bash
# Interactive Debian/Ubuntu VPS setup. Run from a saved file, as root or via sudo.
# This file is also sourceable so tests can exercise phases without changing a host.

use_color() { [[ -t ${1:-1} && "${TERM:-dumb}" != dumb && -z "${NO_COLOR:-}" ]]; }

log() {
    if use_color; then
        printf '\n\033[1;36m==> %s\033[0m\n' "$*"
    else
        printf '\n==> %s\n' "$*"
    fi
}

error() {
    if use_color 2; then
        printf '\033[1;31msetup-vps: %s\033[0m\n' "$*" >&2
    else
        printf 'setup-vps: %s\n' "$*" >&2
    fi
}

die() { error "$*"; exit 1; }

report_failure() {
    local status=$1 line=$2 phase=$3
    error "Failed in $phase at line $line for ${TARGET_USER:-unselected user} (${TARGET_HOME:-home pending}); fix the error and rerun."
    exit "$status"
}

# Only wrap display output, never data captured into config or shell variables.
# pipefail preserves tool failures while the terminal renderer dims noisy logs.
run_tool() {
    if ! use_color; then
        "$@"
        return
    fi
    local status=0
    "$@" 2>&1 | awk '
        {
            gsub(/\033\[[0-9;?]*[[:alpha:]]/, "")
            color = "\033[2;37m"
            if ($0 ~ /(^|[[:space:]])([Ee][Rr][Rr][Oo][Rr]|[Ff][Aa][Ii][Ll][Ee][Dd]|[Ff][Aa][Tt][Aa][Ll])[:[:space:]]|^E:/) color = "\033[1;31m"
            else if ($0 ~ /(^|[[:space:]])[Ww][Aa][Rr][Nn]([Ii][Nn][Gg])?[:[:space:]]|^W:/) color = "\033[33m"
            printf "%s%s\033[0m\n", color, $0
            fflush()
        }
    ' || status=$?
    return "$status"
}

ask() {
    local reply
    while true; do
        read -r -p "$1 [y/N] " reply <&3 || die 'Input closed; setup stopped.'
        case "${reply,,}" in
            y|yes) return 0 ;;
            ''|n|no) return 1 ;;
            *) printf 'Please answer yes or no.\n' >&2 ;;
        esac
    done
}

read_text() {
    local response
    while true; do
        read -r -p "$1: " response <&3 || die 'Input closed; setup stopped.'
        if [[ -n "${response//[[:space:]]/}" ]]; then
            printf -v "$2" '%s' "$response"
            return
        fi
        printf 'Enter a nonempty value.\n' >&2
    done
}

# Reject unsupported hosts before apt or account changes. Derivatives need their
# own repository mapping; guessing a Debian codename can install wrong packages.
detect_platform() {
    local ID VERSION_ID VERSION_CODENAME
    # shellcheck disable=SC1090
    source "${1:-/etc/os-release}"
    case "$ID:$VERSION_ID" in
        debian:12|debian:13|ubuntu:22.04|ubuntu:24.04|ubuntu:26.04) ;;
        *) die "Supported: Debian 12/13 or Ubuntu 22.04/24.04/26.04; found $ID $VERSION_ID." ;;
    esac
    DISTRO=$ID
    CODENAME=${VERSION_CODENAME:?Missing VERSION_CODENAME in os-release}
    ARCH=$(dpkg --print-architecture)
    case "$ARCH" in
        amd64|arm64) ;;
        *) die "Unsupported architecture: $ARCH (expected amd64 or arm64)." ;;
    esac
}

check_docker_conflicts() {
    local package status
    local conflicts=()
    for package in docker.io docker-compose docker-compose-v2 docker-doc docker-buildx podman-docker containerd runc; do
        status=$(dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null) || continue
        [[ "$status" != installed ]] || conflicts+=("$package")
    done
    if ((${#conflicts[@]})); then
        die "Packages conflict with Docker CE: ${conflicts[*]}. Remove them deliberately, then rerun."
    fi
}

# A clean environment and explicit HOME keep sudo's/root's config and credentials
# out of user installations. Start in HOME so mise never trusts a caller's project.
as_user() {
    # HOME and positional arguments expand only after switching users.
    # shellcheck disable=SC2016
    runuser -u "$TARGET_USER" -- env -i \
        HOME="$TARGET_HOME" USER="$TARGET_USER" LOGNAME="$TARGET_USER" \
        XDG_CONFIG_HOME="$TARGET_HOME/.config" XDG_DATA_HOME="$TARGET_HOME/.local/share" \
        XDG_CACHE_HOME="$TARGET_HOME/.cache" \
        PATH="$TARGET_HOME/.local/bin:$TARGET_HOME/.local/share/mise/shims:/usr/local/bin:/usr/bin:/bin" \
        TERM="${TERM:-xterm-256color}" \
        bash -c 'cd -- "$HOME" && exec "$@"' bash "$@"
}

select_user() {
    local username
    CREATE_USER=0
    if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != root ]]; then
        TARGET_USER=$SUDO_USER
    elif ask 'Create a new user with sudo access?'; then
        while true; do
            read_text 'New username (start with a lowercase letter; letters, digits, underscore, hyphen)' username
            if [[ ! "$username" =~ ^[a-z][a-z0-9_-]{0,31}$ ]]; then
                printf 'Use a lowercase username starting with a letter, at most 32 characters.\n' >&2
            elif getent passwd "$username" >/dev/null; then
                printf 'That user already exists; choose a new username.\n' >&2
            else
                break
            fi
        done
        TARGET_USER=$username
        CREATE_USER=1
    else
        TARGET_USER=root
    fi
    if ((CREATE_USER)); then
        log "Will create $TARGET_USER with sudo access after installing account tools"
    else
        resolve_user
    fi
}

resolve_user() {
    local record
    record=$(getent passwd "$TARGET_USER") || die "Unknown user: $TARGET_USER"
    IFS=: read -r _ _ _ _ _ TARGET_HOME _ <<< "$record"
    [[ "$TARGET_HOME" == /* && -d "$TARGET_HOME" ]] || die "Invalid or missing home: $TARGET_HOME"
    TARGET_GROUP=$(id -gn "$TARGET_USER")
    log "Configuring $TARGET_USER in $TARGET_HOME"
}

create_user() {
    ((CREATE_USER)) || return 0
    run_tool adduser --disabled-password --gecos '' "$TARGET_USER"
    # passwd owns the secret prompt after account tools are installed; no
    # password is held in shell state while the package updates run.
    passwd "$TARGET_USER" <&3
    usermod -aG sudo "$TARGET_USER"
    resolve_user
}

apt_install() {
    DEBIAN_FRONTEND=noninteractive run_tool apt-get -o DPkg::Lock::Timeout=120 install -y --no-install-recommends "$@"
}

package_available() {
    LC_ALL=C apt-cache policy "$1" | awk '/Candidate:/ && $2 != "(none)" {found=1} END {exit !found}'
}

select_fetch() {
    if package_available fastfetch; then
        FETCH_TOOL=fastfetch
    elif package_available neofetch; then
        FETCH_TOOL=neofetch
    else
        die 'Neither fastfetch nor neofetch has an apt candidate; check your package sources.'
    fi
}

install_system_packages() {
    log 'Updating package lists and upgrading installed packages'
    run_tool apt-get -o DPkg::Lock::Timeout=120 update
    # Keep existing system configuration during unattended upgrades. Only setup's
    # declared shell exports are replaced below; no automatic reboot is performed.
    DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a run_tool apt-get \
        -o DPkg::Lock::Timeout=120 -o Dpkg::Options::=--force-confold upgrade -y
    select_fetch
    apt_install sudo adduser passwd ca-certificates curl wget gnupg git build-essential \
        unzip zip xz-utils jq ripgrep fd-find bat zoxide btop tmux rsync \
        kitty-terminfo zsh openssh-client util-linux "$FETCH_TOOL"
}

# Stage next to the destination so readers see a complete file after rename.
# Replacing the path also replaces a symlink rather than writing through it.
write_system_file() {
    local destination=$1 temporary
    temporary=$(mktemp "$(dirname "$destination")/.setup-vps.XXXXXX")
    cat > "$temporary"
    chmod 0644 "$temporary"
    mv -fT -- "$temporary" "$destination"
}

install_docker() {
    log 'Installing Docker Engine, Compose, and Buildx'
    install -d -m 0755 /etc/apt/keyrings
    run_tool curl --proto '=https' --tlsv1.2 -fsSL --retry 3 \
        "https://download.docker.com/linux/$DISTRO/gpg" -o "$WORK_DIR/docker.asc"
    [[ -s "$WORK_DIR/docker.asc" ]] || die 'Downloaded Docker signing key is empty.'
    write_system_file /etc/apt/keyrings/docker.asc < "$WORK_DIR/docker.asc"
    write_system_file /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/$DISTRO
Suites: $CODENAME
Components: stable
Architectures: $ARCH
Signed-By: /etc/apt/keyrings/docker.asc
EOF
    run_tool apt-get -o DPkg::Lock::Timeout=120 update
    apt_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    run_tool systemctl enable --now docker.service
    if [[ "$TARGET_USER" != root ]] && ask 'Allow this user to run Docker without sudo? (grants root-level access)'; then
        usermod -aG docker "$TARGET_USER"
    fi
}

install_user_tools() {
    log 'Installing mise and current fzf, eza, and Starship releases'
    as_user mkdir -p "$TARGET_HOME/.local/bin"
    if [[ ! -x "$TARGET_HOME/.local/bin/mise" ]]; then
        # Download before executing so a failed HTTP transfer cannot run a partial
        # installer. mise.run verifies the archive checksum; tool checks stay on.
        run_tool as_user curl --proto '=https' --tlsv1.2 -fsSL --retry 3 \
            https://mise.run -o "$WORK_DIR/mise-install.sh"
        run_tool as_user sh "$WORK_DIR/mise-install.sh"
    fi
    run_tool as_user "$TARGET_HOME/.local/bin/mise" --yes use --global fzf@latest eza@latest starship@latest
    run_tool as_user "$TARGET_HOME/.local/bin/mise" reshim
    # Debian uses these executable names. The links give fzf previews and users
    # the upstream names without changing /usr/bin or replacing cat/ls.
    as_user ln -sfnT /usr/bin/batcat "$TARGET_HOME/.local/bin/bat"
    as_user ln -sfnT /usr/bin/fdfind "$TARGET_HOME/.local/bin/fd"
}

install_plugins() {
    local name url
    log 'Installing Zsh plugins'
    as_user mkdir -p "$WORK_DIR/plugins" "$TARGET_HOME/.local/share/setup-vps"
    while read -r name url; do
        run_tool as_user git clone --quiet --depth 1 -- "$url" "$WORK_DIR/plugins/$name"
    done <<'EOF'
fzf-tab https://github.com/Aloxaf/fzf-tab.git
zsh-autosuggestions https://github.com/zsh-users/zsh-autosuggestions.git
zsh-syntax-highlighting https://github.com/zsh-users/zsh-syntax-highlighting.git
zsh-completions https://github.com/zsh-users/zsh-completions.git
EOF
    # All downloads must succeed before the previous managed plugin tree goes.
    as_user rm -rf -- "$TARGET_HOME/.local/share/setup-vps/plugins"
    as_user mv -T -- "$WORK_DIR/plugins" "$TARGET_HOME/.local/share/setup-vps/plugins"
}

write_user_file() {
    local destination=$1 temporary
    as_user mkdir -p "$(dirname "$destination")"
    temporary=$(as_user mktemp "$(dirname "$destination")/.setup-vps.XXXXXX")
    as_user tee "$temporary" >/dev/null
    as_user chmod 0644 "$temporary"
    as_user mv -fT -- "$temporary" "$destination"
}

configure_zsh() {
    log 'Writing the managed Zsh configuration'
    write_user_file "$TARGET_HOME/.zshrc" <<'EOF'
# Managed by dot-luna/setup-vps; rerunning setup replaces this file.
source "$HOME/.config/setup-vps/zshrc"
EOF
    write_user_file "$TARGET_HOME/.config/setup-vps/zshrc" <<'EOF'
# This configuration is for interactive shells; scripts keep ordinary startup.
[[ -o interactive ]] || return 0
typeset -U path PATH
path=("$HOME/.local/bin" "$HOME/.local/share/mise/shims" $path)
export PATH

HISTFILE="$HOME/.zsh_history"
HISTSIZE=32768
SAVEHIST=32768
setopt APPEND_HISTORY HIST_IGNORE_DUPS HIST_IGNORE_SPACE
bindkey -e
autoload -Uz up-line-or-beginning-search down-line-or-beginning-search
zle -N up-line-or-beginning-search
zle -N down-line-or-beginning-search
bindkey '^[[A' up-line-or-beginning-search
bindkey '^[[B' down-line-or-beginning-search
bindkey '^[OA' up-line-or-beginning-search
bindkey '^[OB' down-line-or-beginning-search

fpath=("$HOME/.local/share/setup-vps/plugins/zsh-completions/src" $fpath)
autoload -Uz compinit
compinit -D

# Activation is generated in this shell so it reflects the current PATH.
eval "$(mise activate zsh)"
source "$HOME/.config/setup-vps/mise-completion.zsh"
eval "$(zoxide init zsh)"
alias cd='z'
alias cdi='zi'
alias ll='eza --long --git --group-directories-first --color=auto --icons=auto'
alias la='eza --long --all --git --group-directories-first --color=auto --icons=auto'
alias lt='eza --tree --level=2 --group-directories-first --color=auto --icons=auto'

export FZF_DEFAULT_COMMAND='fd --type f --hidden --exclude .git'
export FZF_CTRL_T_COMMAND="$FZF_DEFAULT_COMMAND"
export FZF_ALT_C_COMMAND='fd --type d --hidden --exclude .git'
# fzf shell-quotes {} itself; -- keeps selected names from becoming options.
export FZF_CTRL_T_OPTS="--preview 'if [ -d {} ]; then eza --all --color=always -- {}; elif [ -f {} ]; then bat --color=always --style=numbers --paging=never --line-range=:300 -- {}; fi'"
export FZF_ALT_C_OPTS="--preview 'eza --all --color=always -- {}'"
eval "$(fzf --zsh)"

export STARSHIP_CONFIG="$HOME/.config/setup-vps/starship.toml"
eval "$(starship init zsh)"
source "$HOME/.local/share/setup-vps/plugins/fzf-tab/fzf-tab.plugin.zsh"
zstyle ':completion:*' menu no
zstyle ':fzf-tab:complete:(cd|z):*' fzf-preview 'eza --color=always --group-directories-first -- "$realpath"'
source "$HOME/.local/share/setup-vps/plugins/zsh-autosuggestions/zsh-autosuggestions.zsh"
# Highlighting wraps ZLE widgets and must load after the other integrations.
source "$HOME/.local/share/setup-vps/plugins/zsh-syntax-highlighting/zsh-syntax-highlighting.zsh"
EOF
    write_user_file "$TARGET_HOME/.config/setup-vps/starship.toml" <<'EOF'
"$schema" = 'https://starship.rs/config-schema.json'
palette = 'tokyo_night'
add_newline = true
command_timeout = 200
format = '$username$hostname$directory$git_branch$git_commit$git_state$git_status$docker_context$nodejs$bun$python$rust$golang$package$jobs$cmd_duration$line_break$character'

# Keep the account and host visible when switching users on a VPS.
[username]
show_always = true
style_user = 'bold green'
style_root = 'bold red'
format = '\[[$user]($style)\]'

[hostname]
ssh_only = false
ssh_symbol = ''
style = 'bold yellow'
format = '\[[$hostname]($style)\] '

[directory]
truncation_length = 2
truncation_symbol = '…/'
style = 'bold blue'
repo_root_style = 'bold cyan'
repo_root_format = '[$repo_root]($repo_root_style)[$path]($style)[$read_only]($read_only_style) '
read_only = ' ro'

# Text symbols keep the bracketed Luna prompt usable without a Nerd Font.
[git_branch]
symbol = 'git:'
style = 'bold purple'
format = '\[[$symbol$branch]($style)\]'

[git_commit]
tag_symbol = ' tag:'
style = 'bold green'
format = '\[[\($hash$tag\)]($style)\]'

[git_state]
style = 'bold yellow'
format = '\[[$state ($progress_current/$progress_total)]($style)\]'

[git_status]
style = 'bold red'
format = '([\[$all_status$ahead_behind\]]($style))'

[docker_context]
symbol = 'docker:'
style = 'bold cyan'
format = '\[[$symbol$context]($style)\]'

[nodejs]
symbol = 'node:'
style = 'bold green'
format = '\[[$symbol($version)]($style)\]'

[bun]
symbol = 'bun:'
style = 'bold white'
format = '\[[$symbol($version)]($style)\]'

[python]
symbol = 'py:'
style = 'bold yellow'
format = '\[[${symbol}${pyenv_prefix}(${version})(\($virtualenv\))]($style)\]'

[rust]
symbol = 'rs:'
style = 'bold red'
format = '\[[$symbol($version)]($style)\]'

[golang]
symbol = 'go:'
style = 'bold cyan'
format = '\[[$symbol($version)]($style)\]'

[package]
symbol = 'pkg:'
style = 'bold bright-white'
format = '\[[$symbol$version]($style)\]'

[jobs]
symbol = 'jobs:'
number_threshold = 1
style = 'bold blue'
format = '\[[$symbol$number]($style)\]'

[cmd_duration]
style = 'bold yellow'
format = '\[[took $duration]($style)\]'

[character]
success_symbol = '[>](bold green)'
error_symbol = '[>](bold red)'
vimcmd_symbol = '[<](bold yellow)'

# Explicit Tokyo Night Night colors work without a terminal theme or Luna files.
# https://github.com/folke/tokyonight.nvim/blob/main/extras/lua/tokyonight_night.lua
[palettes.tokyo_night]
black = '#15161e'
red = '#f7768e'
green = '#9ece6a'
yellow = '#e0af68'
blue = '#7aa2f7'
purple = '#9d7cd8'
magenta = '#bb9af7'
cyan = '#7dcfff'
white = '#a9b1d6'
bright-black = '#414868'
bright-red = '#ff899d'
bright-green = '#9fe044'
bright-yellow = '#faba4a'
bright-blue = '#8db0ff'
bright-purple = '#c7a9ff'
bright-magenta = '#c7a9ff'
bright-cyan = '#a4daff'
bright-white = '#c0caf5'
EOF
    as_user "$TARGET_HOME/.local/bin/mise" completion zsh > "$WORK_DIR/mise-completion.zsh"
    [[ -s "$WORK_DIR/mise-completion.zsh" ]] || die 'mise generated empty Zsh completion.'
    write_user_file "$TARGET_HOME/.config/setup-vps/mise-completion.zsh" < "$WORK_DIR/mise-completion.zsh"
    as_user zsh -n "$TARGET_HOME/.config/setup-vps/zshrc"
    chsh -s /usr/bin/zsh "$TARGET_USER"
}

git_identity_value() {
    local status=0
    as_user git config --global --get "user.$1" || status=$?
    ((status == 0 || status == 1)) || die 'Cannot read Git config; fix it before rerunning.'
}

collect_git_identity() {
    log 'Collecting Git identity'
    GIT_NAME=$TARGET_USER
    GIT_EMAIL=''
    # A prospective account has no home yet. Existing config is inspected with
    # Git itself when available, never by trying to parse its files by hand.
    if ((CREATE_USER == 0)) && command -v git >/dev/null && command -v runuser >/dev/null; then
        local name
        name=$(git_identity_value name)
        [[ -z "${name//[[:space:]]/}" ]] || GIT_NAME=$name
        GIT_EMAIL=$(git_identity_value email)
    fi
    printf 'Git name: %s\n' "$GIT_NAME"
    if [[ -z "${GIT_EMAIL//[[:space:]]/}" ]]; then
        read_text 'Git email' GIT_EMAIL
    fi
}

configure_git() {
    local value
    log 'Applying Git identity'
    # Recheck after package installation: an existing identity, including one
    # added while updates ran, takes precedence over the collected candidates.
    value=$(git_identity_value name)
    if [[ -z "${value//[[:space:]]/}" ]]; then
        as_user git config --global -- user.name "$GIT_NAME"
    fi
    value=$(git_identity_value email)
    if [[ -z "${value//[[:space:]]/}" ]]; then
        as_user git config --global -- user.email "$GIT_EMAIL"
    fi
}

verify_fetch() {
    # Neofetch 7.1 exits 1 for --version even on success. --stdout exercises the
    # installed tool and returns a useful status without requiring image support.
    if [[ "$FETCH_TOOL" == neofetch ]]; then
        as_user neofetch --stdout
    else
        as_user fastfetch --version
    fi
}

verify_install() {
    local record
    log 'Verifying installed tools and Docker'
    record=$(getent passwd "$TARGET_USER") || die "Unknown configured user: $TARGET_USER"
    [[ "${record##*:}" == /usr/bin/zsh ]] || die "Default shell for $TARGET_USER is not /usr/bin/zsh."
    as_user test -r "$TARGET_HOME/.zshrc" || die "Missing Zsh startup file: $TARGET_HOME/.zshrc"
    as_user test -r "$TARGET_HOME/.config/setup-vps/zshrc" || die "Missing Zsh config: $TARGET_HOME/.config/setup-vps/zshrc"
    systemctl is-active --quiet docker.service
    docker info >/dev/null
    run_tool docker compose version
    run_tool docker buildx version
    run_tool as_user "$TARGET_HOME/.local/bin/mise" --version
    run_tool as_user fzf --version
    run_tool as_user eza --version
    run_tool as_user starship --version
    run_tool verify_fetch
    if ! run_tool as_user zsh -ic '[[ $+functions[z] == 1 && $+functions[fzf-history-widget] == 1 && $+functions[_zsh_highlight] == 1 && $+functions[fzf-tab-complete] == 1 && $+functions[_zsh_autosuggest_start] == 1 ]]' <&3; then
        die "Zsh startup failed for $TARGET_USER; check $TARGET_HOME/.zshrc and $TARGET_HOME/.config/setup-vps/zshrc."
    fi
}

cleanup_packages() {
    log 'Removing unused apt dependencies'
    DEBIAN_FRONTEND=noninteractive run_tool apt-get -o DPkg::Lock::Timeout=120 autoremove -y
}

print_summary() {
    log 'Setup complete'
    printf 'Configured user: %s\nHome: %s\nZsh startup file: %s/.zshrc\nDefault shell: /usr/bin/zsh\nSystem info: %s\n' "$TARGET_USER" "$TARGET_HOME" "$TARGET_HOME" "$FETCH_TOOL"
    printf 'The current terminal keeps its shell until you start a new login session.\n'
    printf 'Open the configured login shell with: sudo -iu %q\n' "$TARGET_USER"
    printf 'Reconnect to activate updated group membership.\n'
    if [[ "$TARGET_USER" != root ]]; then
        printf 'For SSH login as %s, add your computer\047s public key to %s/.ssh/authorized_keys.\n' "$TARGET_USER" "$TARGET_HOME"
    fi
    if [[ -f /var/run/reboot-required || -f /run/reboot-required ]]; then
        printf 'A reboot is required by installed updates; reboot when ready.\n'
    fi
}

run_setup() {
    # Collect non-secret choices before apt work; account and Git tools may not
    # exist until installation completes, so apply these choices afterwards.
    select_user
    collect_git_identity
    install_system_packages
    create_user
    configure_git
    WORK_DIR=$(mktemp -d /tmp/setup-vps.XXXXXX)
    trap 'rm -rf -- "$WORK_DIR"' EXIT
    chown "$TARGET_USER:$TARGET_GROUP" "$WORK_DIR"
    install_docker
    install_user_tools
    install_plugins
    configure_zsh
    cleanup_packages
    verify_install
    print_summary
}

main() {
    set -Eeuo pipefail
    umask 022
    trap 'report_failure "$?" "$LINENO" "${FUNCNAME[0]:-main}"' ERR
    case "${1:-}" in
        -h|--help)
            printf 'Usage: bash setup-deb.sh\nInteractive Debian/Ubuntu VPS setup; requires root or sudo and a terminal.\n'
            return ;;
        '') ;;
        *) die 'Unknown argument. Usage: bash setup-deb.sh' ;;
    esac
    (($# == 0)) || die 'Unexpected arguments. Usage: bash setup-deb.sh'
    detect_platform /etc/os-release
    if ((EUID != 0)); then
        command -v sudo >/dev/null || die 'Run as root; sudo is not installed.'
        exec sudo -- bash "$(realpath -- "${BASH_SOURCE[0]}")"
    fi
    [[ -d /run/systemd/system ]] || die 'A running systemd host is required.'
    # /dev/tty keeps prompts independent from redirected output or script input.
    exec 3<>/dev/tty || die 'Run from a saved file in an interactive terminal.'
    check_docker_conflicts
    run_setup
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
