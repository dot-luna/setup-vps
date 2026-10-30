"""Exercise installer phases without apt, root access, accounts, or networking."""

import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "setup-deb.sh"

# Real tools may only write into the test's temporary home. Individual tests
# replace additional commands before invoking phases that would affect a host.
LOCAL_USER = r'''
as_user() {
    HOME="$TARGET_HOME" XDG_CONFIG_HOME="$TARGET_HOME/.config" \
        GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL="$TARGET_HOME/.gitconfig" \
        command "$@"
}
'''


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="setup-vps-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.home = self.directory / "selected home"
        self.home.mkdir()
        self.work = self.directory / "work"
        self.work.mkdir()

    def bash(self, body, input="", *, env=None, expected=0):
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
            and key not in {"SUDO_USER", "BASH_ENV", "ENV", "ZDOTDIR"}
        }
        environment.update(
            SETUP_SCRIPT=str(SCRIPT),
            TARGET_HOME=str(self.home),
            TARGET_USER="alice",
            WORK_DIR=str(self.work),
            TEST_DIRECTORY=str(self.directory),
            HOME=str(self.directory),
            LC_ALL="C",
        )
        if env:
            environment.update({key: str(value) for key, value in env.items()})
        result = subprocess.run(
            [
                "bash",
                "--noprofile",
                "--norc",
                "-c",
                'set -Eeuo pipefail\nsource "$SETUP_SCRIPT"\nexec 3<&0\n' + body,
            ],
            input=input,
            capture_output=True,
            text=True,
            env=environment,
            timeout=20,
        )
        if expected is not None:
            self.assertEqual(
                result.returncode,
                expected,
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
        return result

    def release(self, distro, version, codename="bookworm"):
        path = self.directory / "os-release"
        path.write_text(
            f"ID={distro}\nVERSION_ID={version}\nVERSION_CODENAME={codename}\n"
        )
        return path

    def test_yes_no_defaults_case_insensitivity_and_invalid_input(self):
        for answer, expected in (("\n", "no"), ("n\n", "no"), ("NO\n", "no"),
                                 ("y\n", "yes"), ("YES\n", "yes")):
            with self.subTest(answer=answer):
                result = self.bash(
                    'if ask "Continue?"; then printf yes; else printf no; fi',
                    answer,
                )
                self.assertEqual(result.stdout, expected)
        result = self.bash(
            'if ask "Continue?"; then printf yes; else printf no; fi',
            "maybe\nyes\n",
        )
        self.assertEqual(result.stdout, "yes")
        self.assertIn("Please answer yes or no", result.stderr)

    def test_yes_no_eof_aborts_instead_of_selecting_a_default(self):
        result = self.bash(
            'if ask "Continue?"; then :; fi\nprintf unsafe', expected=1
        )
        self.assertIn("Input closed", result.stderr)
        self.assertNotIn("unsafe", result.stdout)

    def test_read_text_rejects_empty_values_and_preserves_literal_characters(self):
        result = self.bash(
            'value=old\nread_text "Git name" value\nprintf "<%s>" "$value"',
            "\n \t \n  Jane \\ Smith $HOME  \n",
        )
        self.assertEqual(result.stdout, "<Jane \\ Smith $HOME>")
        self.assertEqual(result.stderr.count("Enter a nonempty value"), 2)

    def test_read_text_eof_aborts(self):
        result = self.bash('read_text "Name" value\nprintf unsafe', expected=1)
        self.assertIn("Input closed", result.stderr)
        self.assertNotIn("unsafe", result.stdout)

    def test_supported_platforms_and_architectures(self):
        for distro, version, codename in (
            ("debian", "12", "bookworm"),
            ("debian", "13", "trixie"),
            ("ubuntu", "22.04", "jammy"),
            ("ubuntu", "24.04", "noble"),
            ("ubuntu", "26.04", "resolute"),
        ):
            for architecture in ("amd64", "arm64"):
                with self.subTest(distro=distro, version=version, arch=architecture):
                    release = self.release(distro, version, codename)
                    result = self.bash(
                        r'''
dpkg() { printf '%s\n' "$TEST_ARCH"; }
detect_platform "$TEST_RELEASE"
printf '%s:%s:%s' "$DISTRO" "$CODENAME" "$ARCH"
''',
                        env={"TEST_RELEASE": release, "TEST_ARCH": architecture},
                    )
                    self.assertEqual(
                        result.stdout, f"{distro}:{codename}:{architecture}"
                    )

    def test_unknown_distributions_releases_and_architectures_fail(self):
        for distro, version in (("debian", "11"), ("ubuntu", "20.04"), ("mint", "22")):
            with self.subTest(distro=distro, version=version):
                result = self.bash(
                    'dpkg() { printf amd64; }\ndetect_platform "$TEST_RELEASE"',
                    env={"TEST_RELEASE": self.release(distro, version)},
                    expected=1,
                )
                self.assertIn("Supported:", result.stderr)
        result = self.bash(
            'dpkg() { printf armhf; }\ndetect_platform "$TEST_RELEASE"',
            env={"TEST_RELEASE": self.release("debian", "12")},
            expected=1,
        )
        self.assertIn("Unsupported architecture: armhf", result.stderr)

    def test_root_can_decline_new_user_without_changing_accounts(self):
        result = self.bash(
            r'''
getent() { printf 'root:x:0:0:root:%s:/bin/bash\n' "$TARGET_HOME"; }
id() { printf root; }
adduser() { die 'unexpected account creation'; }
usermod() { die 'unexpected group modification'; }
select_user
printf '\nselected=%s:%s:%s\n' "$TARGET_USER" "$TARGET_GROUP" "$TARGET_HOME"
''',
            "no\n",
        )
        self.assertIn(f"selected=root:root:{self.home}", result.stdout)

    def test_sudo_invocation_selects_original_user_without_prompting(self):
        result = self.bash(
            r'''
SUDO_USER=alice
getent() { [[ "$*" == 'passwd alice' ]]; printf 'alice:x:1001:1001::%s:/bin/bash\n' "$TARGET_HOME"; }
id() { [[ "$*" == '-gn alice' ]]; printf alice; }
ask() { die 'unexpected prompt'; }
adduser() { die 'unexpected account creation'; }
usermod() { die 'unexpected group modification'; }
select_user
printf '\nselected=%s:%s\n' "$TARGET_USER" "$TARGET_GROUP"
'''
        )
        self.assertIn("selected=alice:alice", result.stdout)

    def test_new_user_validation_password_and_sudo_membership(self):
        result = self.bash(
            r'''
getent() {
    [[ "$1" == passwd ]]
    if [[ "$2" == taken ]]; then
        printf 'taken:x:1000:1000::/home/taken:/bin/bash\n'
    elif [[ "$2" == alice && -e "$WORK_DIR/created" ]]; then
        printf 'alice:x:1001:1001::%s:/bin/bash\n' "$TARGET_HOME"
    else
        return 2
    fi
}
adduser() { printf 'adduser:%s\n' "$*"; touch "$WORK_DIR/created"; }
passwd() { printf 'passwd:%s\n' "$*"; }
usermod() { printf 'usermod:%s\n' "$*"; }
id() { printf alice; }
select_user
printf '\nselected=%s\n' "$TARGET_USER"
''',
            "yes\nBadName\n_alice\ntaken\nalice\n",
        )
        self.assertEqual(result.stderr.count("Use a lowercase username"), 2)
        self.assertIn("That user already exists", result.stderr)
        self.assertIn("adduser:--disabled-password --gecos  alice", result.stdout)
        self.assertIn("passwd:alice", result.stdout)
        self.assertIn("usermod:-aG sudo alice", result.stdout)
        self.assertIn("selected=alice", result.stdout)

    def test_invalid_user_home_stops_before_configuration(self):
        result = self.bash(
            r'''
SUDO_USER=alice
getent() { printf 'alice:x:1001:1001::relative/home:/bin/bash\n'; }
select_user
printf unsafe
''',
            expected=1,
        )
        self.assertIn("Invalid or missing home", result.stderr)
        self.assertNotIn("unsafe", result.stdout)

    def test_fetch_prefers_fastfetch_and_falls_back_to_neofetch(self):
        for available, expected in (("fastfetch neofetch", "fastfetch"), ("neofetch", "neofetch")):
            with self.subTest(available=available):
                result = self.bash(
                    r'''
apt-cache() {
    if [[ " $TEST_AVAILABLE " == *" $2 "* ]]; then
        printf '  Candidate: 1.2.3\n'
    else
        printf '  Installed: (none)\n  Candidate: (none)\n'
    fi
}
select_fetch
printf '%s' "$FETCH_TOOL"
''',
                    env={"TEST_AVAILABLE": available},
                )
                self.assertEqual(result.stdout, expected)

    def test_fetch_missing_both_candidates_fails(self):
        result = self.bash(
            r'''
apt-cache() { printf '  Installed: 1.0\n  Candidate: (none)\n'; }
select_fetch
''',
            expected=1,
        )
        self.assertIn("Neither fastfetch nor neofetch", result.stderr)

    def test_package_candidate_lookup_uses_stable_locale(self):
        self.bash(
            r'''
unset LC_ALL
apt-cache() {
    [[ "$LC_ALL" == C ]] || return 8
    printf '  Candidate: 1.0\n'
}
package_available fastfetch
'''
        )

    def test_install_verification_accepts_neofetch_with_nonzero_version_status(self):
        result = self.bash(
            r'''
FETCH_TOOL=neofetch
systemctl() { :; }
docker() { :; }
as_user() {
    if [[ "$1" == neofetch ]]; then
        [[ "$2" != --version ]] || return 1
        [[ "$2" == --stdout ]] || return 2
        printf 'OS: Debian\n'
    fi
}
verify_install
'''
        )
        self.assertIn("OS: Debian", result.stdout)

    def test_system_install_updates_upgrades_and_installs_selected_fetch(self):
        result = self.bash(
            r'''
apt-get() {
    printf 'apt:%s;frontend=%s;restart=%s\n' "$*" "${DEBIAN_FRONTEND:-}" "${NEEDRESTART_MODE:-}"
}
package_available() { [[ "$1" == neofetch ]]; }
install_system_packages
'''
        )
        lines = [line for line in result.stdout.splitlines() if line.startswith("apt:")]
        self.assertEqual(len(lines), 3)
        self.assertIn(" update;", lines[0])
        self.assertIn("--force-confold upgrade -y", lines[1])
        self.assertIn("frontend=noninteractive;restart=a", lines[1])
        self.assertIn(" install -y ", lines[2])
        for package in ("sudo", "git", "zsh", "kitty-terminfo", "bat", "zoxide", "neofetch"):
            self.assertIn(package, lines[2].split(";")[0].split())
        self.assertNotIn("fastfetch", lines[2])

    def test_docker_conflicts_only_block_installed_packages(self):
        result = self.bash(
            r'''
dpkg-query() {
    case "${@: -1}" in
        docker.io) printf config-files ;;
        docker-compose) printf not-installed ;;
        *) return 1 ;;
    esac
}
check_docker_conflicts
printf clear
'''
        )
        self.assertEqual(result.stdout, "clear")
        result = self.bash(
            r'''
dpkg-query() {
    case "${@: -1}" in
        docker.io|podman-docker|containerd) printf installed ;;
        *) return 1 ;;
    esac
}
check_docker_conflicts
''',
            expected=1,
        )
        self.assertIn("docker.io podman-docker containerd", result.stderr)
        self.assertIn("Remove them deliberately", result.stderr)

    def test_docker_repository_key_packages_and_optional_group_membership(self):
        for distro, codename, architecture, user, answer in (
            ("debian", "trixie", "amd64", "root", ""),
            ("ubuntu", "noble", "arm64", "alice", "yes\n"),
        ):
            with self.subTest(distro=distro, user=user):
                result = self.bash(
                    r'''
install() { printf 'install:%s\n' "$*"; }
curl() {
    [[ "$*" == *"https://download.docker.com/linux/$DISTRO/gpg"* ]]
    printf 'test signing key\n' > "$WORK_DIR/docker.asc"
}
write_system_file() {
    case "$1" in
        /etc/apt/keyrings/docker.asc) cat > "$WORK_DIR/captured-key" ;;
        /etc/apt/sources.list.d/docker.sources) cat > "$WORK_DIR/captured-sources" ;;
        *) die "unexpected destination: $1" ;;
    esac
}
apt-get() { printf 'apt:%s\n' "$*"; }
apt_install() { printf 'packages:%s\n' "$*"; }
systemctl() { printf 'systemctl:%s\n' "$*"; }
usermod() { printf 'usermod:%s\n' "$*"; }
install_docker
cat "$WORK_DIR/captured-sources"
''',
                    answer,
                    env={"DISTRO": distro, "CODENAME": codename, "ARCH": architecture, "TARGET_USER": user},
                )
                self.assertEqual((self.work / "captured-key").read_text(), "test signing key\n")
                self.assertIn(f"URIs: https://download.docker.com/linux/{distro}\n", result.stdout)
                self.assertIn(f"Suites: {codename}\n", result.stdout)
                self.assertIn(f"Architectures: {architecture}\n", result.stdout)
                self.assertIn("Signed-By: /etc/apt/keyrings/docker.asc\n", result.stdout)
                self.assertIn("packages:docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin", result.stdout)
                self.assertIn("systemctl:enable --now docker.service", result.stdout)
                if user == "root":
                    self.assertNotIn("usermod:", result.stdout)
                else:
                    self.assertIn("usermod:-aG docker alice", result.stdout)

    def test_empty_docker_key_never_changes_repository_or_installs_packages(self):
        result = self.bash(
            r'''
DISTRO=debian
install() { :; }
curl() { : > "$WORK_DIR/docker.asc"; }
write_system_file() { die 'unexpected repository write'; }
apt-get() { die 'unexpected apt'; }
apt_install() { die 'unexpected install'; }
install_docker
''',
            expected=1,
        )
        self.assertIn("Downloaded Docker signing key is empty", result.stderr)
        self.assertNotIn("unexpected", result.stderr)

    def test_selected_user_environment_excludes_caller_credentials_and_project(self):
        result = self.bash(
            r'''
runuser() {
    [[ "$1" == -u && "$2" == alice && "$3" == -- ]]
    shift 3
    command "$@"
}
export GIT_CONFIG_GLOBAL=/caller/credentials SSH_AUTH_SOCK=/caller/agent
export XDG_CONFIG_HOME=/caller/config AWS_SECRET_ACCESS_KEY=caller-secret
export MISE_TRUSTED_CONFIG_PATHS=/caller/project
cd "$WORK_DIR"
as_user bash -c 'printf "working=%s\n" "$PWD"; env'
'''
        )
        values = dict(line.split("=", 1) for line in result.stdout.splitlines())
        self.assertEqual(values["working"], str(self.home))
        self.assertEqual(values["HOME"], str(self.home))
        self.assertEqual(values["USER"], "alice")
        self.assertEqual(values["LOGNAME"], "alice")
        self.assertEqual(values["XDG_CONFIG_HOME"], str(self.home / ".config"))
        self.assertEqual(values["XDG_DATA_HOME"], str(self.home / ".local/share"))
        self.assertEqual(values["XDG_CACHE_HOME"], str(self.home / ".cache"))
        self.assertTrue(values["PATH"].startswith(str(self.home / ".local/bin") + ":"))
        for key in ("GIT_CONFIG_GLOBAL", "SSH_AUTH_SOCK", "AWS_SECRET_ACCESS_KEY", "MISE_TRUSTED_CONFIG_PATHS"):
            self.assertNotIn(key, values)

    def test_failed_mise_download_never_executes_partial_installer(self):
        self.bash(
            r'''
as_user() {
    case "$1" in
        mkdir) command "$@" ;;
        curl) printf 'partial download' > "$WORK_DIR/mise-install.sh"; return 22 ;;
        sh) touch "$WORK_DIR/executed" ;;
        *) die 'unexpected command after failed download' ;;
    esac
}
install_user_tools
''',
            expected=22,
        )
        self.assertFalse((self.work / "executed").exists())

    def test_failed_plugin_clone_keeps_previous_managed_plugins(self):
        plugins = self.home / ".local/share/setup-vps/plugins"
        plugins.mkdir(parents=True)
        marker = plugins / "previous-plugin"
        marker.write_text("working installation")
        self.bash(
            r'''
as_user() {
    case "$1" in
        mkdir) command "$@" ;;
        git) return 128 ;;
        *) die 'unexpected replacement after failed clone' ;;
    esac
}
install_plugins
''',
            expected=128,
        )
        self.assertEqual(marker.read_text(), "working installation")

    def test_plugin_rerun_replaces_the_managed_export(self):
        plugins = self.home / ".local/share/setup-vps/plugins"
        plugins.mkdir(parents=True)
        (plugins / "obsolete").write_text("stale")
        self.bash(
            r'''
as_user() {
    if [[ "$1" == git ]]; then
        mkdir -p -- "${@: -1}"
    else
        command "$@"
    fi
}
install_plugins
'''
        )
        self.assertEqual(
            sorted(path.name for path in plugins.iterdir()),
            ["fzf-tab", "zsh-autosuggestions", "zsh-completions", "zsh-syntax-highlighting"],
        )

    @unittest.skipUnless(shutil.which("git"), "git is required")
    def test_existing_global_git_identity_does_not_prompt_or_rewrite(self):
        original = '[user]\n\tname = Existing Name\n\temail = old@example.com\n# keep me\n'
        config = self.home / ".gitconfig"
        config.write_text(original)
        self.bash(LOCAL_USER + "configure_git")
        self.assertEqual(config.read_text(), original)

    @unittest.skipUnless(shutil.which("git"), "git is required")
    def test_git_only_prompts_for_missing_identity(self):
        (self.home / ".gitconfig").write_text('[user]\n\temail = old@example.com\n')
        result = self.bash(
            LOCAL_USER
            + r'''
configure_git
printf 'name=%s\n' "$(as_user git config --global --get user.name)"
printf 'email=%s\n' "$(as_user git config --global --get user.email)"
read -r untouched <&3
printf 'remaining=%s\n' "$untouched"
''',
            "\nJane Smith\nsentinel\n",
        )
        self.assertIn("name=Jane Smith", result.stdout)
        self.assertIn("email=old@example.com", result.stdout)
        self.assertIn("remaining=sentinel", result.stdout)
        self.assertIn("Enter a nonempty value", result.stderr)

    @unittest.skipUnless(shutil.which("git"), "git is required")
    def test_malformed_git_config_is_an_error_instead_of_an_identity_prompt(self):
        config = self.home / ".gitconfig"
        original = '[user\nname = damaged\n'
        config.write_text(original)
        result = self.bash(LOCAL_USER + "configure_git", "New Name\n", expected=1)
        self.assertIn("Cannot read Git config", result.stderr)
        self.assertEqual(config.read_text(), original)

    def test_ssh_decline_does_not_create_files(self):
        self.bash(LOCAL_USER + "configure_ssh", "no\n")
        self.assertFalse((self.home / ".ssh").exists())

    @unittest.skipUnless(shutil.which("ssh-keygen") and shutil.which("git"), "OpenSSH and Git are required")
    def test_new_ssh_key_has_permissions_and_only_public_material_is_printed(self):
        (self.home / ".gitconfig").write_text('[user]\nemail = alice@example.com\n')
        result = self.bash(
            r'''
as_user() {
    if [[ "$1" == ssh-keygen ]]; then
        shift
        command ssh-keygen -N '' "$@"
    else
        HOME="$TARGET_HOME" XDG_CONFIG_HOME="$TARGET_HOME/.config" \
            GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL="$TARGET_HOME/.gitconfig" command "$@"
    fi
}
configure_ssh
''',
            "yes\n",
        )
        ssh = self.home / ".ssh"
        private = ssh / "id_ed25519"
        public = ssh / "id_ed25519.pub"
        self.assertEqual(stat.S_IMODE(ssh.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(public.stat().st_mode), 0o644)
        self.assertTrue(public.read_text().startswith("ssh-ed25519 "))
        self.assertTrue(public.read_text().endswith(" alice@example.com\n"))
        self.assertIn(public.read_text().strip(), result.stdout)
        self.assertIn(str(private), result.stdout)
        self.assertNotIn("BEGIN OPENSSH PRIVATE KEY", result.stdout)
        self.assertNotIn(private.read_text(), result.stdout)

    def test_existing_or_partial_ssh_keys_and_dangling_links_are_preserved(self):
        for kind in ("both", "private", "public", "private-link", "public-link"):
            with self.subTest(kind=kind):
                home = self.directory / kind
                ssh = home / ".ssh"
                ssh.mkdir(parents=True)
                private = ssh / "id_ed25519"
                public = ssh / "id_ed25519.pub"
                if kind in ("both", "private"):
                    private.write_text("existing private bytes")
                if kind in ("both", "public"):
                    public.write_text("existing public bytes")
                if kind.endswith("-link"):
                    (private if kind == "private-link" else public).symlink_to(ssh / "missing")
                before = {p.name: (p.is_symlink(), os.readlink(p) if p.is_symlink() else p.read_bytes()) for p in ssh.iterdir()}
                result = self.bash(
                    r'''
ask() { die 'unexpected SSH prompt'; }
as_user() { die 'unexpected SSH command'; }
configure_ssh
''',
                    env={"TARGET_HOME": home},
                )
                after = {p.name: (p.is_symlink(), os.readlink(p) if p.is_symlink() else p.read_bytes()) for p in ssh.iterdir()}
                self.assertEqual(before, after)
                self.assertIn("Keeping existing SSH key paths", result.stdout)

    def test_atomic_writes_replace_symlinks_and_do_not_touch_their_targets(self):
        for function in ("write_user_file", "write_system_file"):
            with self.subTest(function=function):
                folder = self.home / function
                folder.mkdir()
                original = folder / "unrelated"
                original.write_text("unrelated content\n")
                destination = folder / "managed config"
                destination.symlink_to(original)
                self.bash(
                    LOCAL_USER
                    + r'''
printf 'first version\n' | "$TEST_WRITER" "$TEST_DESTINATION"
printf 'replacement\n' | "$TEST_WRITER" "$TEST_DESTINATION"
''',
                    env={"TEST_WRITER": function, "TEST_DESTINATION": destination},
                )
                self.assertFalse(destination.is_symlink())
                self.assertEqual(destination.read_text(), "replacement\n")
                self.assertEqual(original.read_text(), "unrelated content\n")
                self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o644)
                self.assertEqual(sorted(p.name for p in folder.iterdir()), ["managed config", "unrelated"])

    @unittest.skipUnless(shutil.which("zsh"), "Zsh is required")
    def test_generated_zsh_loads_integrations_and_aliases_after_repeated_setup(self):
        binaries = self.home / ".local/bin"
        binaries.mkdir(parents=True)
        programs = {
            "mise": r'''#!/bin/sh
case "$1" in
    completion) printf '# generated completion\n' ;;
    activate) printf 'print -r -- mise >> "$SETUP_TEST_TRACE"\n' ;;
    *) exit 2 ;;
esac
''',
            "zoxide": r'''#!/bin/sh
printf 'function z() { builtin cd "$@"; }; function zi() { :; }; print -r -- zoxide >> "$SETUP_TEST_TRACE"\n'
''',
            "fzf": r'''#!/bin/sh
printf 'function fzf-history-widget() { :; }; zle -N fzf-history-widget; print -r -- fzf >> "$SETUP_TEST_TRACE"\n'
''',
            "starship": r'''#!/bin/sh
printf 'print -r -- starship >> "$SETUP_TEST_TRACE"\n'
''',
        }
        for name, content in programs.items():
            path = binaries / name
            path.write_text(content)
            path.chmod(0o755)
        plugins = self.home / ".local/share/setup-vps/plugins"
        plugin_sources = {
            "fzf-tab/fzf-tab.plugin.zsh": r'''
(( ${+functions[_main_complete]} && ${+functions[fzf-history-widget]} )) || return 9
print -r -- fzf-tab >> "$SETUP_TEST_TRACE"
''',
            "zsh-autosuggestions/zsh-autosuggestions.zsh": 'print -r -- autosuggestions >> "$SETUP_TEST_TRACE"\n',
            "zsh-syntax-highlighting/zsh-syntax-highlighting.zsh": 'function _zsh_highlight() { :; }; print -r -- highlighting >> "$SETUP_TEST_TRACE"\n',
            "zsh-completions/src/_fixture": '#compdef fixture\n_fixture() { :; }\n',
        }
        for name, content in plugin_sources.items():
            path = plugins / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        trace = self.work / "integration trace"
        destination = self.home / "directory with spaces"
        destination.mkdir()
        result = self.bash(
            LOCAL_USER
            + r'''
chsh() { [[ "$*" == '-s /usr/bin/zsh alice' ]]; }
configure_zsh
printf 'stale user additions\n' >> "$TARGET_HOME/.zshrc"
printf 'stale managed additions\n' >> "$TARGET_HOME/.config/setup-vps/zshrc"
configure_zsh
env -i HOME="$TARGET_HOME" PATH="$TARGET_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" \
    TERM=xterm-256color SETUP_TEST_TRACE="$TEST_TRACE" SETUP_TEST_DEST="$TEST_DEST" \
    zsh -d -i -c '
        [[ ${+functions[z]} == 1 && ${+functions[fzf-history-widget]} == 1 && ${+functions[_zsh_highlight]} == 1 ]] || exit 10
        print -rl -- "cd_alias=$aliases[cd]" "cdi_alias=$aliases[cdi]"
        print -rl -- "ll_alias=$aliases[ll]" "la_alias=$aliases[la]" "lt_alias=$aliases[lt]"
        print -rl -- "history=$HISTFILE" "files=$FZF_DEFAULT_COMMAND" "directories=$FZF_ALT_C_COMMAND"
        cd -- "$SETUP_TEST_DEST"
        print -r -- "arrived=$PWD"
    '
''',
            env={"TEST_TRACE": trace, "TEST_DEST": destination},
        )
        self.assertEqual(
            trace.read_text().splitlines(),
            ["mise", "zoxide", "fzf", "starship", "fzf-tab", "autosuggestions", "highlighting"],
        )
        self.assertIn("cd_alias=z\n", result.stdout)
        self.assertIn("cdi_alias=zi\n", result.stdout)
        self.assertIn("ll_alias=eza --long --git", result.stdout)
        self.assertIn("la_alias=eza --long --all --git", result.stdout)
        self.assertIn("lt_alias=eza --tree --level=2", result.stdout)
        self.assertIn(f"history={self.home}/.zsh_history", result.stdout)
        self.assertIn("files=fd --type f --hidden --exclude .git", result.stdout)
        self.assertIn("directories=fd --type d --hidden --exclude .git", result.stdout)
        self.assertIn(f"arrived={destination}", result.stdout)
        self.assertNotIn("stale", (self.home / ".zshrc").read_text())
        self.assertNotIn("stale", (self.home / ".config/setup-vps/zshrc").read_text())


if __name__ == "__main__":
    unittest.main()
